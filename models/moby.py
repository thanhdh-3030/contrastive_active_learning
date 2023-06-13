# --------------------------------------------------------
# Swin Transformer
# Copyright (c) 2021 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Zhenda Xie
# --------------------------------------------------------

from matplotlib.pyplot import axes
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
# import torch.distributed as dist

from diffdist import functional
import math
#from resnet import Transformer3, Transformer


def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    # if (mean < a - 2 * std) or (mean > b + 2 * std):
    #     warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
    #                   "The distribution of values may be incorrect.",
    #                   stacklevel=2)
    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor

def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # type: (Tensor, float, float, float, float) -> Tensor
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)

class DINOHead(nn.Module):
    def __init__(self, in_dim, out_dim, use_bn=False, norm_last_layer=True, nlayers=3, hidden_dim=2048, bottleneck_dim=256):
        super().__init__()
        nlayers = max(nlayers, 1)
        if nlayers == 1:
            self.mlp = nn.Linear(in_dim, bottleneck_dim)
        else:
            layers = [nn.Linear(in_dim, hidden_dim)]
            if use_bn:
                layers.append(nn.BatchNorm1d(hidden_dim))
            layers.append(nn.GELU())
            for _ in range(nlayers - 2):
                layers.append(nn.Linear(hidden_dim, hidden_dim))
                if use_bn:
                    layers.append(nn.BatchNorm1d(hidden_dim))
                layers.append(nn.GELU())
            layers.append(nn.Linear(hidden_dim, bottleneck_dim))
            self.mlp = nn.Sequential(*layers)
        self.apply(self._init_weights)
        self.last_layer = nn.utils.weight_norm(nn.Linear(bottleneck_dim, out_dim, bias=False))
        self.last_layer.weight_g.data.fill_(1)
        if norm_last_layer:
            self.last_layer.weight_g.requires_grad = False

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.mlp(torch.squeeze(x))
        x = nn.functional.normalize(x, dim=-1, p=2)
        x = self.last_layer(x)
        return x

class MultiCropWrapper(nn.Module):
    """
    Perform forward pass separately on each resolution input.
    The inputs corresponding to a single resolution are clubbed and single
    forward is run on the same resolution inputs. Hence we do several
    forward passes = number of different resolutions used. We then
    concatenate all the output features and run the head forward on these
    concatenated features.
    """
    def __init__(self, backbone, head):
        super(MultiCropWrapper, self).__init__()
        # disable layers dedicated to ImageNet labels classification
        backbone.fc, backbone.head = nn.Identity(), nn.Identity()
        self.backbone = backbone
        self.head = head

    def forward(self, x):
        # convert to list
        if not isinstance(x, list):
            x = [x]
        idx_crops = torch.cumsum(torch.unique_consecutive(
            torch.tensor([inp.shape[-1] for inp in x]),
            return_counts=True,
        )[1], 0)
        start_idx, output = 0, torch.empty(0).to(x[0].device)
        for end_idx in idx_crops:
            # print(x[start_idx: end_idx])
            # print(torch.cat(x[start_idx: end_idx]).shape)
            _out = self.backbone(torch.cat(x[start_idx: end_idx]))
            # The output is a tuple with XCiT model. See:
            # https://github.com/facebookresearch/xcit/blob/master/xcit.py#L404-L405
            if isinstance(_out, tuple):
                _out = _out[0]
            # accumulate outputs
            output = torch.cat((output, _out))
            start_idx = end_idx
        # Run the head forward on the concatenated features.
        return self.head(output), output

   
    
class MoBYMLP(nn.Module):
    def __init__(self, in_dim=128, inner_dim=4096, out_dim= 128, num_layers=2):
        super(MoBYMLP, self).__init__()
        
        # hidden layers
        linear_hidden = [nn.Identity()]
        for i in range(num_layers - 1):
            linear_hidden.append(nn.Linear(in_dim if i == 0 else inner_dim, inner_dim))
            linear_hidden.append(nn.BatchNorm1d(inner_dim))
            linear_hidden.append(nn.ReLU(inplace=True))
        self.linear_hidden = nn.Sequential(*linear_hidden)

        self.linear_out = nn.Linear(in_dim if num_layers == 1 else inner_dim, out_dim) if num_layers >= 1 else nn.Identity()

    def forward(self, x):
        x = x.contiguous()
        x = x.view(x.size()[0], -1)
        x = self.linear_hidden(x)
        x = self.linear_out(x)

        return x

class MoBY(nn.Module):
    def __init__(self,
                 cfg,
                 encoder,
                 encoder_k,
                 classifier,
                 contrast_momentum=0.99,
                 contrast_temperature=0.2,
                 contrast_num_negative=4096,
                 proj_num_layers=2,
                 pred_num_layers=2,
                 num_classes=10,
                 contrast_num_positive=128,
                 **kwargs):
        super().__init__()
        
        self.cfg = cfg
        
        self.encoder = encoder
        self.encoder_k = encoder_k
        self.classifier = classifier
        
        self.contrast_momentum = contrast_momentum
        self.contrast_temperature = contrast_temperature
        self.contrast_num_negative = contrast_num_negative
        self.contrast_num_positive=contrast_num_positive
        self.proj_num_layers = proj_num_layers
        self.pred_num_layers = pred_num_layers
        self.num_classes=num_classes
        self.projector = MoBYMLP(in_dim=self.encoder.num_features, num_layers=proj_num_layers)
        self.projector_k = MoBYMLP(in_dim=self.encoder.num_features, num_layers=proj_num_layers)
        self.predictor = MoBYMLP(in_dim=128,num_layers=pred_num_layers)

        for param_q, param_k in zip(self.encoder.parameters(), self.encoder_k.parameters()):
            param_k.data.copy_(param_q.data)  # initialize
            param_k.requires_grad = False  # not update by gradient

        for param_q, param_k in zip(self.projector.parameters(), self.projector_k.parameters()):
            param_k.data.copy_(param_q.data)
            param_k.requires_grad = False

        # if self.cfg.MODEL.SWIN.NORM_BEFORE_MLP == 'bn':
        #     nn.SyncBatchNorm.convert_sync_batchnorm(self.encoder)
        #     nn.SyncBatchNorm.convert_sync_batchnorm(self.encoder_k)

        nn.SyncBatchNorm.convert_sync_batchnorm(self.projector)
        nn.SyncBatchNorm.convert_sync_batchnorm(self.projector_k)
        nn.SyncBatchNorm.convert_sync_batchnorm(self.predictor)

        self.K = int(self.cfg.DATA_TRAINING_IMAGES * 1.  / 
                  self.cfg.DATA_BATCH_SIZE) * self.cfg.TRAIN_EPOCHS
        self.k = int(self.cfg.DATA_TRAINING_IMAGES * 1.  / 
                  self.cfg.DATA_BATCH_SIZE) * self.cfg.TRAIN_START_EPOCH

        # create the queue
        self.register_buffer("queue1", torch.randn(128, self.contrast_num_negative))
        self.register_buffer("queue2", torch.randn(128, self.contrast_num_negative))
        self.queue1 = F.normalize(self.queue1, dim=0)
        self.queue2 = F.normalize(self.queue2, dim=0)

        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))
        for i in range(self.num_classes):
            self.register_buffer("cls_queue1_"+str(i), torch.randn(128, self.contrast_num_positive))
            self.register_buffer("cls_queue2_"+str(i), torch.randn(128, self.contrast_num_positive))
            self.register_buffer("cls_queue_ptr"+str(i),torch.zeros(1,dtype=torch.long))
            exec("self.cls_queue1_"+str(i) + '=' + 'nn.functional.normalize(' + "self.cls_queue1_"+str(i) + ',dim=0)')
            exec("self.cls_queue2_"+str(i) + '=' + 'nn.functional.normalize(' + "self.cls_queue2_"+str(i) + ',dim=0)')

    @torch.no_grad()
    def _momentum_update_key_encoder(self):
        """
        Momentum update of the key encoder
        """
        _contrast_momentum = 1. - (1. - self.contrast_momentum) * (np.cos(np.pi * self.k / self.K) + 1) / 2.
        self.k = self.k + 1

        for param_q, param_k in zip(self.encoder.parameters(), self.encoder_k.parameters()):
            param_k.data = param_k.data * _contrast_momentum + param_q.data * (1. - _contrast_momentum)

        for param_q, param_k in zip(self.projector.parameters(), self.projector_k.parameters()):
            param_k.data = param_k.data * _contrast_momentum + param_q.data * (1. - _contrast_momentum)

    @torch.no_grad()
    def _dequeue_and_enqueue(self, keys1, keys2):
        # gather keys before updating queue
        # keys1 = dist_collect(keys1)
        # keys2 = dist_collect(keys2)

        batch_size = keys1.shape[0]

        ptr = int(self.queue_ptr)
        assert self.contrast_num_negative % batch_size == 0  # for simplicity

        # replace the keys at ptr (dequeue and enqueue)
        self.queue1[:, ptr:ptr + batch_size] = keys1.T
        self.queue2[:, ptr:ptr + batch_size] = keys2.T
        ptr = (ptr + batch_size) % self.contrast_num_negative  # move pointer

        self.queue_ptr[0] = ptr
    @torch.no_grad()
    def _dequeue_and_enqueue_label(self, keys1, keys2,labels):
        # gather keys before updating queue
        # keys1 = dist_collect(keys1)
        # keys2 = dist_collect(keys2)
        for i in range(self.num_classes):
            cls_mask=(labels==i)
            if(torch.sum(cls_mask).item()==0):continue
            this_keys1=keys1[cls_mask]
            # print(this_keys1.shape)
            this_keys2=keys2[cls_mask]
            batch_size=this_keys1.shape[0]
            this_ptr = int(eval('self.cls_queue_ptr'+str(i)))
            if(this_ptr+batch_size)<=self.contrast_num_positive:
                # replace the keys at ptr (dequeue and enqueue)
                eval("self.cls_queue1_"+str(i))[:,this_ptr:this_ptr + batch_size]=this_keys1.T
                eval("self.cls_queue2_"+str(i))[:,this_ptr:this_ptr + batch_size]=this_keys2.T
            else:
                inx=self.contrast_num_positive-this_ptr
                head_keys1=this_keys1[:inx]
                tail_keys1=this_keys1[inx:]
                head_keys2=this_keys2[:inx]
                tail_keys2=this_keys2[inx:]
                eval("self.cls_queue1_"+str(i))[:,this_ptr:]=head_keys1.T
                eval("self.cls_queue1_"+str(i))[:,:len(tail_keys1)]=tail_keys1.T
                eval("self.cls_queue2_"+str(i))[:,this_ptr:]=head_keys2.T
                eval("self.cls_queue2_"+str(i))[:,:len(tail_keys1)]=tail_keys2.T
            eval("self.cls_queue_ptr"+str(i))[0]=(this_ptr + batch_size) % self.contrast_num_positive # move pointer

    def contrastive_loss(self, q, k, queue):

        # positive logits: Nx1
        l_pos = torch.einsum('nc,nc->n', [q, k]).unsqueeze(-1)
        # negative logits: NxK
        l_neg = torch.einsum('nc,ck->nk', [q, queue.clone().detach()])

        # logits: Nx(1+K)
        logits = torch.cat([l_pos, l_neg], dim=1)

        # apply temperature
        logits /= self.contrast_temperature

        # labels: positive key indicators
        labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()

        # return F.cross_entropy(logits, labels, reduction='none')
        return F.cross_entropy(logits, labels, reduction='mean')

    def contrastive_loss_q(self, q, k):

        # positive logits: Nx1
        l_pos = q #torch.einsum('nc,nc->nc', [q, k])
        # negative logits: NxK
        # l_neg = torch.einsum('nc,ck->nk', [q, queue.clone().detach()])

        # logits: Nx(1+K)
        # logits = torch.cat([l_pos, l_neg], dim=1)

        # apply temperature
        logits = l_pos / self.contrast_temperature

        # labels: positive key indicators
        labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()

        return F.cross_entropy(logits, labels, reduction='none')
    def _compute_contrast_loss(self, l_pos, l_neg):
        N = l_pos.size(0)
        logits = torch.cat((l_pos,l_neg),dim=1)
        logits /= self.contrast_temperature
        labels = torch.zeros((N,),dtype=torch.long).cuda()
        return F.cross_entropy(logits,labels)
    
    def forward(self, im_1, im_2, targets=None):
        feat_1 = self.encoder(im_1)  # queries: NxC
        proj_1 = self.projector(torch.squeeze(feat_1))
        pred_1 = self.predictor(proj_1)
        pred_1 = F.normalize(pred_1, dim=1)

        feat_2 = self.encoder(im_2)
        proj_2 = self.projector(torch.squeeze(feat_2))
        pred_2 = self.predictor(proj_2)
        pred_2 = F.normalize(pred_2, dim=1)

        # if targets is not None:
        #     outputs = self.classifier(feat_2)
        # compute key features
        with torch.no_grad():  # no gradient to keys
            self._momentum_update_key_encoder()  # update the key encoder

            feat_1_ng = self.encoder_k(im_1)  # keys: NxC
            proj_1_ng = self.projector_k(feat_1_ng)
            proj_1_ng = F.normalize(proj_1_ng, dim=1)

            feat_2_ng = self.encoder_k(im_2)
            proj_2_ng = self.projector_k(feat_2_ng)
            proj_2_ng = F.normalize(proj_2_ng, dim=1)

        if targets is None:
            # compute loss
            loss = self.contrastive_loss(pred_1, proj_2_ng, self.queue2) \
                + self.contrastive_loss(pred_2, proj_1_ng, self.queue1)
            loss_q = self.contrastive_loss_q(pred_2, proj_1_ng)
            self._dequeue_and_enqueue(proj_1_ng, proj_2_ng)
            return loss, feat_2, loss_q
        else:
            un_ctr_loss = self.contrastive_loss(pred_1, proj_2_ng, self.queue2) \
                + self.contrastive_loss(pred_2, proj_1_ng, self.queue1)
            la_ctr_loss=0
            valid_class=0
            proto_list=[]
            for i in range(self.num_classes):
                cls_mask=(targets==i)
                if(torch.sum(cls_mask).item()==0):continue
                cls_pred_1=pred_1[cls_mask]
                cls_proto=torch.mean(cls_pred_1,dim=0)
                proto_list.append(cls_proto)
                valid_class=valid_class+1

            for i in range(self.num_classes):
                cls_mask=(targets==i)
                if(torch.sum(cls_mask).item()==0):continue
                cls_pred_1=pred_1[cls_mask]
                bs=cls_pred_1.shape[0]
                # query=torch.mean(cls_pred_1,dim=0)
                query=cls_pred_1
                pos_keys=proj_2_ng[cls_mask]
                # pos_keys=eval('self.cls_queue2_'+str(i))[:,:bs].clone().detach()
                # pos_keys=eval('self.cls_queue2_'+str(i)).clone().detach()
                # pos_keys=eval('self.cls_queue1_'+str(i))[:,:bs].clone().detach()
                # pos_keys=proto_list[i].unsqueeze(0).repeat(bs, 1)
                # pos_logit=torch.sum(cls_pred_1*pos_keys.T, dim=1, keepdim=True)
                # pos_logit=query.unsqueeze(1)*pos_keys
                neg_logit=0
                all_classes=[m for m in range(self.num_classes)]
                all_classes.remove(i)
                neg_classes=all_classes.copy()
                neg_feat_list=[]
                for neg_class in neg_classes:
                    neg_feat_list.append(eval('self.cls_queue2_'+str(neg_class))[:,:128].clone().detach())
                    # neg_feat_list.append(eval('self.cls_queue1_'+str(neg_class))[:,:256].clone().detach())
                    # neg_keys=eval('self.cls_queue1_'+str(neg_class)).clone().detach()
                    # neg_keys=eval('self.cls_queue2_'+str(neg_class)).clone().detach()    
                    # neg_logit += cls_pred_1@neg_keys
                    # neg_logit += query.unsqueeze(1)*neg_keys                              
                neg_feats=torch.cat(neg_feat_list,dim=1)
                # la_ctr_loss += self.contrastive_loss(cls_pred_1,pos_keys,neg_feats)
                # la_ctr_loss += self.contrastive_loss(cls_pred_1,pos_keys.T,self.queue2[:,:256].clone().detach())
                # neg_feats=neg_feats.T.repeat(bs,1,1)
                # all_feats=torch.cat((pos_keys.T.unsqueeze(1),neg_feats),dim=1)
                # logits=torch.cosine_similarity(cls_pred_1.unsqueeze(1),all_feats,dim=2)
                # la_ctr_loss += F.cross_entropy(logits/self.contrast_temperature,torch.zeros(bs).long().cuda())
                la_ctr_loss += self.contrastive_loss(query, pos_keys, neg_feats)
                # # ctr_loss=self.contrastive_loss_q(query,pos_keys)
                # ctr_loss +=self._compute_contrast_loss(pos_logit,neg_logit)
            la_ctr_loss=la_ctr_loss/valid_class
            # ctr_loss=0
            # loss=(un_ctr_loss+ctr_loss)/2
            # loss=un_ctr_loss
            self._dequeue_and_enqueue_label(proj_1_ng,proj_2_ng,targets)
            self._dequeue_and_enqueue(proj_1_ng, proj_2_ng)
            return un_ctr_loss,la_ctr_loss,feat_2,None
