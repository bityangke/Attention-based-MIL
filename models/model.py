import os
import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
from .backbone import Backbone
import numpy as np


class Attention_Net(nn.Module):
    def __init__(self, fc_input_dims=512, fc_mid_dims=256, dropout=False, n_classes=1):
        super(Attention_Net, self).__init__()
        self.fc_input_dims = fc_input_dims
        self.fc_mid_dims = fc_mid_dims
        self.n_classes = n_classes
        self.dropout = dropout
        self.att_module = [
            nn.Linear(self.fc_input_dims, self.fc_mid_dims),
            nn.ReLU(),
        ]
        if self.dropout:
            self.att_module.append(nn.Dropout(0.25))

        self.att_module.append(nn.Linear(self.fc_mid_dims, n_classes))
        self.att_module = nn.Sequential(*self.att_module)

    def forward(self, x):
        return self.att_module(x), x


class Attention_Net_Gated(nn.Module):
    def __init__(self, L=1024, D=256, dropout=False, n_classes=1):
        super(Attn_Net_Gated, self).__init__()
        self.attention_a = [
            nn.Linear(L, D),
            nn.Tanh()]

        self.attention_b = [nn.Linear(L, D),
                            nn.Sigmoid()]
        if dropout:
            self.attention_a.append(nn.Dropout(0.25))
            self.attention_b.append(nn.Dropout(0.25))

        self.attention_a = nn.Sequential(*self.attention_a)
        self.attention_b = nn.Sequential(*self.attention_b)

        self.attention_c = nn.Linear(D, n_classes)

    def forward(self, x):
        a = self.attention_a(x)
        b = self.attention_b(x)
        A = a.mul(b)
        A = self.attention_c(A)  # N x n_classes
        return A, x


class ABMIL(nn.Module):
    def __init__(self, encoder_name='mobilenetv2_10', gate=False, dropout=False, k_sample=10, n_classes=2, instance_loss_fn=None):
        super(ABMIL, self).__init__()
        self.encoder_name = encoder_name
        self.encoder = Backbone.model_zoo[self.encoder_name]

        self.fc_params = [1280, 512, 256]
        fc = [nn.Linear(self.fc_params[0], self.fc_params[1]), nn.ReLU()]
        if gate:
            attention_net = Attention_Net_Gated(L=self.fc_params[1], D=self.fc_params[2], dropout=dropout, n_classes=1)
        else:
            attention_net = Attention_Net(fc_input_dims=self.fc_params[1], fc_mid_dims=self.fc_params[2], dropout=dropout, n_classes=1)
        fc.append(attention_net)
        self.attention_net = nn.Sequential(*fc)
        self.classifier = nn.Linear(self.fc_params[1], n_classes)
        self.k_sample = k_sample
        self.n_calsses = n_classes
        self.instance_loss_fn = instance_loss_fn
        self.instance_classifiers = None
        if self.instance_loss_fn is not None:  # ?????????????????? pos or neg
            self.instance_classifiers = nn.Linear(self.fc_params[1], 2)

    def relocate(self, model_parallel=False):
        device_nums = torch.cuda.device_count()
        if model_parallel and device_nums >= 2:  # ??????????????????
            self.encoder.relocate()
            self.attention_net.to("cuda:{}".format(device_nums - 1))  # ??????????????????
            self.classifier.to("cuda:{}".format(device_nums - 1))
            if self.instance_classifiers is not None:
                self.instance_classifiers.to("cuda:{}".format(device_nums - 1))
        else:  # ???????????????batch_size = 1
            self.encoder.cuda()
            self.attention_net.cuda()
            self.classifier.cuda()
            if self.instance_classifiers is not None:
                self.instance_classifiers.cuda()

    @staticmethod
    def create_positive_targets(length, device):
        return torch.full((length,), 1, device=device).long()

    @staticmethod
    def create_negative_targets(length, device):
        return torch.full((length,), 0, device=device).long()

    def instance_eval(self, A, h, classifier, k_sample, is_pos=True):
        device = h.device
        if len(A.shape) == 1:
            A = A.view(1, -1)
        topk_p_ids = torch.topk(A, k_sample)[1][-1]  # ?????????k???instance
        topk_p = torch.index_select(h, dim=0, index=topk_p_ids)
        topk_n_ids = torch.topk(-A, k_sample)[1][-1] # ?????????K???instance
        topk_n = torch.index_select(h, dim=0, index=topk_n_ids)
        # print(is_pos, k_sample)
        if is_pos:
            p_targets = self.create_positive_targets(k_sample, device)
            n_targets = self.create_negative_targets(k_sample, device)
        else:  # neg bag??? ????????????
            p_targets = self.create_negative_targets(k_sample, device)
            n_targets = self.create_negative_targets(k_sample, device)

        all_targets = torch.cat([p_targets, n_targets], dim=0)
        all_instances = torch.cat([topk_p, topk_n], dim=0)
        logits = classifier(all_instances)
        all_preds = torch.topk(logits, 1, dim=1)[1].squeeze(1)
        # all_preds = F.softmax(logits, dim=1)[:, 1]
        instance_loss = self.instance_loss_fn(logits, all_targets)
        return instance_loss, all_preds, all_targets

    def instance_eval_high_attend(self, A, h, classifier, k_sample, is_pos=True):
        device = h.device
        if len(A.shape) == 1:
            A = A.view(1, -1)
        topk_p_ids = torch.topk(A, k_sample)[1][-1]
        topk_p = torch.index_select(h, dim=0, index=topk_p_ids)
        p_targets = self.create_positive_targets(k_sample, device) if is_pos else \
                    self.create_negative_targets(k_sample, device)
        logits = classifier(topk_p)
        p_preds = torch.topk(logits, 1, dim=1)[1].squeeze(1)
        instance_loss = self.instance_loss_fn(logits, p_targets)
        return instance_loss, p_preds, p_targets

    def forward(self, x, label=None, instance_eval=False, instance_eval_high_attend=False, inference_only=False):
        h = self.encoder(x)
        weights, h = self.attention_net(h)    # N * 1
        weights = torch.transpose(weights, 1, 0)   # 1 * N

        att_weights = weights
        weights = F.softmax(weights, dim=1)
        assert not(instance_eval and instance_eval_high_attend), print("The two options must be mutually exclusive")
        instance_loss = 0
        all_preds = []
        all_targets = []
        if instance_eval:
            # print(self.k_sample)
            instance_loss, all_preds, all_targets = self.instance_eval(weights, h, self.instance_classifiers,
                                                                       self.k_sample if label == 1 else 4*self.k_sample, is_pos=(label == 1))
            all_preds = all_preds.cpu().numpy()
            all_targets = all_targets.cpu().numpy()

        if instance_eval_high_attend:
            instance_loss, all_preds, all_targets = self.instance_eval_high_attend \
                (weights, h, self.instance_classifiers, self.k_sample, is_pos=(label == 1))
            all_preds = all_preds.cpu().numpy()
            all_targets = all_targets.cpu().numpy()

        M = torch.mm(weights, h)
        logits = self.classifier(M)
        Y_hat = torch.topk(logits, 1, dim=1)[1]
        Y_prob = F.softmax(logits, dim=1)

        if inference_only:
            infer_results = dict()
            infer_results["weights"] = att_weights
            infer_results["features"] = h
            all_porbs = F.softmax(self.instance_classifiers(h), dim=1)[:, 1]
            infer_results["probs"] = all_porbs
            return logits, infer_results

        if instance_eval or instance_eval_high_attend:
            instance_results = {'instance_loss': instance_loss, 'inst_labels': np.array(all_targets),
                            'inst_preds': np.array(all_preds)}
        else:
            instance_results = {}

        return logits, Y_hat, Y_prob, instance_results, att_weights


def train(model):
    num_iters = 5
    batch_size = 400
    model.train()
    loss_fn = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=0.001, weight_decay=1e-4)
    for each_iter in range(num_iters):
        inputs = torch.randn(batch_size, 3, 128, 128).to("cuda:0")

        outputs, Y_hat, Y_prob, isinstance_results, weights = model(inputs)
        print(Y_hat, Y_prob)
        labels = torch.randint(0, 2, (1,)).to(outputs.device)
        print(outputs.size(), weights.size())
        optimizer.zero_grad()
        loss_fn(outputs, labels).backward()
        optimizer.step()


if __name__ == '__main__':
    os.environ['CUDA_VISIBLE_DEVICES'] = '2'     # ???GPU
    # model = ABMIL(fc_input_dims=2048).cuda()
    model = ABMIL(instance_loss_fn=nn.CrossEntropyLoss())
    model.relocate(model_parallel=False)
    num_epochs = 10
    for each_epoch in range(num_epochs):
        train(model)
