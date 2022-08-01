import gc
import math
import logging
import torch
import torch.nn as nn
from torch import autocast
import torchaudio
import torch.nn.functional as F
import pytorch_lightning as pl
import numpy as np
from utils.transforms import interp_targets, powspace, SpecAugment, MixUp, AudioTransforms
from models.WavLM import WavLM, WavLMConfig
from dataclasses import dataclass

@dataclass
class ModelConfig():
    backbone:str='base'
    freeze_feature_extractor:bool=True
    label_smoothing:float=0.1
    wav2vec2_weight_decay:float = 0.1
    wavlm_weight_decay:float = 1e-4
    specaugment:bool=False
    time_masking_percentage:float=0.02
    feature_masking_percentage:float=0.02
    n_feature_masks:int=2
    n_time_masks:int=2
    n_classes:int=2
    fuzzy_cs_labels:bool=False
    buffer_length:int=9
    buffer_lower_bound:float=0.8
    ord:float=0.25
    soft_units:bool=False
    soft_unit_layer_train:int=4
    soft_units_context:int=2
    cuda_device = torch.device("cuda:0" if torch.cuda.is_available else "cpu")
    class_weights:torch.Tensor= torch.Tensor([1, 1, 1, 1, 1]).to(cuda_device)
    custom_cross_entropy:bool=False
    mixup:bool=False
    mixup_prob:float=1.0
    mixup_size:float=1.0
    beta:float=1.0
    audio_transforms:bool=True
    speed_min:float=0.8
    speed_max:float=1.2

class LitCSDetector(pl.LightningModule):
    def __init__(self, learning_rate=1e-4, model_config=None):
        super().__init__()
        self.save_hyperparameters()

        if model_config==None: self.model_config = ModelConfig()
        else: self.model_config = model_config

        self.label_smoothing = model_config.label_smoothing
        self.specaugment = model_config.specaugment
        self.soft_units = model_config.soft_units
        self.soft_units_context = model_config.soft_units_context
        self.soft_unit_layer_train = model_config.soft_unit_layer_train
        self.class_wights = model_config.class_weights
        self.mixup = model_config.mixup
        self.audio_transforms = model_config.audio_transforms

        if model_config.custom_cross_entropy:
             self.custom_cross_entropy = CustomLabelSmoothingCrossEntropy(epsilon=model_config.label_smoothing)
        else: self.custom_cross_entropy = None
        factor = 1

        if self.audio_transforms: self.audio_transformer = AudioTransforms(
                                                    speed_min=model_config.speed_min,
                                                    speed_max=model_config.speed_max)

        if self.mixup: self.mixer=MixUp(
                            mixup_prob=model_config.mixup_prob, 
                            mixup_size=model_config.mixup_size, 
                            beta=model_config.beta)

        if self.specaugment: self.spec_augmenter=SpecAugment(
                                    feature_masking_percentage=model_config.feature_masking_percentage,
                                    time_masking_percentage=model_config.time_masking_percentage,
                                    n_feature_masks=model_config.n_feature_masks,
                                    n_time_masks=model_config.n_time_masks
                                    )

        assert model_config.backbone in ["base","large", "xlsr", "wavlm-large"], f'model: {model_config.backbone} not supported.'

        # Diffrent weight decays for wav2vec2 and wavlm
        if self.model_config.backbone in ["base","large", "xlsr"]: self.weight_decay = model_config.wav2vec2_weight_decay
        elif self.model_config.backbone in ["wavlm-large"]: self.weight_decay = model_config.wavlm_weight_decay

        if model_config.backbone == "base":
            bundle = torchaudio.pipelines.WAV2VEC2_BASE
            self.backbone = bundle.get_model()
            embed_dim = 768

        if model_config.backbone == "large":
            bundle = torchaudio.pipelines.WAV2VEC2_LARGE
            self.backbone = bundle.get_model()
            embed_dim = 1024

        if model_config.backbone == "xlsr":
            bundle = torchaudio.pipelines.WAV2VEC2_XLSR53
            self.backbone = bundle.get_model()
            embed_dim = 1024

        if model_config.backbone == "wavlm-large":
            checkpoint = torch.load('/home/gfrost/projects/penguin/models/WavLM-Large.pt')
            cfg = WavLMConfig(checkpoint['cfg'])
            self.backbone = WavLM(cfg)
            self.backbone.load_state_dict(checkpoint['model'])
            embed_dim = 1024

        self.head = nn.Linear(embed_dim*factor, model_config.n_classes)

        if self.soft_units: 
            self.soft_head = nn.Linear(model_config.n_classes + model_config.n_classes*2*model_config.soft_units_context, 
                                                model_config.n_classes)

            nn.init.xavier_uniform_(self.soft_head.weight, gain=1 / math.sqrt(2))

        nn.init.xavier_uniform_(self.head.weight, gain=1 / math.sqrt(2))

        if model_config.freeze_feature_extractor:
            for param in self.backbone.feature_extractor.parameters():
                param.requires_grad = False

    def forward(self, x, l, y=None, transforms=False, overide=False):
        
        if self.audio_transforms and transforms: 
            x = self.audio_transformer.forward(x.cpu()).to(x.device)
            y = torch.round(interp_targets(y, x.size(-1)).float()).long()

        if self.model_config.backbone in ['wavlm-large']:

            padding_masks = get_padding_masks_from_length(x, l)
            x, padding_masks, lengths = self.backbone.custom_feature_extractor(x, padding_masks)

            if self.mixup and transforms:

                y = interp_targets(y, torch.max(lengths))
                x, y = self.mixer.forward(x, 
                                    lengths, 
                                    F.one_hot(y.to(torch.long), num_classes=self.model_config.n_classes).float())

            if self.specaugment and transforms: x = self.spec_augmenter.forward(x)

            x, lengths = self.backbone.transformer_encoder(x, padding_mask=padding_masks, ret_lengths=True)
            # x, lengths = self.backbone.extract_features(x, padding_mask=padding_masks, ret_lengths=True) # OG 

        else: 

            x, lengths = self.backbone.feature_extractor(x, l)
            if self.mixup and transforms: x, y = self.mixer.forward(x)
            if self.specaugment and transforms: x = self.spec_augmenter.forward(x)
            # Include intermediate layer for more syntactic infomation (wav2vec-U)
            x = self.backbone.encoder(x, lengths)

        # if self.mixup: x, y = self.mixer.forward(x) # Trying mixup after transformer encoder...

        x = self.head(x)

        if (self.current_epoch > self.soft_unit_layer_train and self.soft_units) or overide:
            x = cat_neighbors_for_soft_units(x, self.model_config.soft_units_context)
            x = self.soft_head(x)

        if self.mixup and transforms:
            return x, lengths, y

        else: return x, lengths, None

    def training_step(self, batch, batch_idx):
        x, x_l, y, y_l = batch
        y_hat, lengths, y_interp = self.forward(x, x_l, y, transforms=True)
        loss, y = aggregate_bce_loss(y_hat, y, y_interp, lengths, self.model_config, self.custom_cross_entropy)
        self.log('train/loss', loss)
        return {'loss':loss, 'y_hat':y_hat.detach(), 'y':y.detach(), 'lengths':lengths.detach()}
    
    def training_epoch_end(self, out):
        y_hat = torch.cat([x['y_hat'].view(-1, self.model_config.n_classes)[get_unpadded_idxs(x['lengths'])] for x in out])
        y = torch.cat([x['y'].view(-1, self.model_config.n_classes)[get_unpadded_idxs(x['lengths'])]for x in out])
        accuracy = (torch.softmax(y_hat, dim=-1).argmax(dim=-1) == y.argmax(dim=-1)).sum().float() / float(y.size(0))
        self.log("train/train_acc", accuracy, on_epoch=True)
        gc.collect()

    def validation_step(self, batch, batch_idx):
        x, x_l, y, y_l = batch
        y_hat, lengths, _ = self.forward(x, x_l, transforms=False)
        loss, y = aggregate_bce_loss(y_hat, y, None, lengths, self.model_config, self.custom_cross_entropy)
        self.log('val/loss', loss.detach().item())
        return {'y_hat':y_hat.detach(), 'y':y.detach(), 'lengths':lengths.detach()}

    def validation_epoch_end(self, out):
        y_hat = torch.cat([x['y_hat'].view(-1, self.model_config.n_classes)[get_unpadded_idxs(x['lengths'])] for x in out])
        y = torch.cat([x['y'].view(-1, self.model_config.n_classes)[get_unpadded_idxs(x['lengths'])]for x in out])
        accuracy = (torch.softmax(y_hat, dim=-1).argmax(dim=-1) == y.argmax(dim=-1)).sum().float() / float(y.size(0))
        self.log("val/val_acc", accuracy, on_epoch=True)

    def configure_optimizers(self):
        # if self.model_config.backbone in ["wavlm-large"]:
        #     optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, self.parameters()), 
        #                                 lr=1,
        #                                 momentum=0.9,
        #                                 weight_decay=self.weight_decay)
        # else:
        optimizer = torch.optim.AdamW(filter(lambda p: p.requires_grad, self.parameters()), 
                                    lr=1, 
                                    weight_decay=self.weight_decay)

        # lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.95)

        # linear warmup + exponential decay
        lr_scheduler = torch.optim.lr_scheduler.LambdaLR(
            optimizer=optimizer, lr_lambda=lambda step: rate(step, self.hparams.learning_rate, 1, 1000)
        )

        return {'optimizer':optimizer, 'lr_scheduler':{'scheduler':lr_scheduler, 'interval':'step'}}

def cat_neighbors_for_soft_units(x, soft_units_context):

    x_l = torch.zeros_like(x).repeat(1, 1, soft_units_context)
    x_r = torch.zeros_like(x).repeat(1, 1, soft_units_context)

    for i in range(soft_units_context): x_l[:, i+1:, i*x.size(-1):(i+1)*x.size(-1)] = x[:, :-(i+1), :]
    for i in range(soft_units_context): x_r[:, :-(i+1), i*x.size(-1):(i+1)*x.size(-1)] = x[:, i+1:, :]

    return torch.cat([x, x_l, x_r], dim=-1)

def get_padding_masks_from_length(source, lengths):
    indexs = torch.arange(source.size(-1)).unsqueeze(dim=0).repeat(lengths.size(0), 1).to(lengths.device)
    lengths = lengths.unsqueeze(dim=-1).repeat(1, indexs.size(-1))
    padding_mask = indexs > lengths
    return padding_mask


def get_attention_masks(lengths, max_len, nheads):
    masks1d = (torch.arange(max_len).cuda()[None, :] > lengths[:, None]).repeat(nheads, 1).unsqueeze(dim=-1)
    masks1d = masks1d.float()
    masks2d = torch.matmul(masks1d,masks1d.transpose(-2, -1))
    return masks2d.bool()
    
def get_unpadded_idxs(lengths):
    max_len = torch.max(lengths)
    return torch.cat([torch.arange(max_len*i, max_len*i+l) for i, l in enumerate(lengths)]).to(torch.long)

def aggregate_bce_loss(y_hat, y, y_interp, lengths, cfg, custom_cross_entropy=None):
    
    # In case we apply mixup
    if y_interp == None:
        y = interp_targets(y, torch.max(lengths))
    else: y = y_interp

    idxs = get_unpadded_idxs(lengths)

    if cfg.fuzzy_cs_labels: 
     y = fuzzy_cs_labels(y, lengths, cfg.n_classes, cfg.buffer_length, cfg.buffer_lower_bound, cfg.ord)

    if len(y.shape) < 3: 
        y = F.one_hot(y.to(torch.long), num_classes=cfg.n_classes).float()

    if custom_cross_entropy == None:
        loss = F.cross_entropy(y_hat.view(-1, cfg.n_classes)[idxs], 
                                y.view(-1, cfg.n_classes)[idxs],
                                label_smoothing=cfg.label_smoothing, 
                                weight=cfg.class_weights)
    else: 
        loss = custom_cross_entropy(y_hat.view(-1, cfg.n_classes)[idxs], 
                                y.view(-1, cfg.n_classes)[idxs])
    return loss, y

def fuzzy_cs_labels(targets, lengths, num_classes, buffer_length=7, buffer_lower_bound=0.7, ord=1):

    assert buffer_lower_bound >= 0.5, "buffer_lower_bound cannot be less than 0.5"

    grad = torch.gradient(targets, dim=-1)[0]
    switches = torch.where(grad!=0)

    l_s = targets[switches[0][1::2], switches[1][0::2]].long()
    r_s = targets[switches[0][1::2], switches[1][1::2]].long()

    l_index = switches[1][0::2]
    r_index = switches[1][1::2]
    b_index = switches[0][1::2]

    if ord == 1: inter_probs_ =  torch.linspace(buffer_lower_bound, 1, buffer_length)
    else: inter_probs_ = torch.tensor([buffer_lower_bound]*buffer_length) + \
            powspace(0, 1-buffer_lower_bound, power=ord, num=buffer_length)
    
    inter_probs_ = torch.stack([inter_probs_, 1-inter_probs_], dim=-1)
    inter_probs = torch.cat([torch.flip(inter_probs_, dims=[0, 1]), inter_probs_], dim=0)

    half_way = len(inter_probs)//2 + 1
    switch_from_pad = r_index+half_way+1 <= lengths[b_index]
    b_index = b_index[switch_from_pad]
    l_index = l_index[switch_from_pad]
    l_s = l_s[switch_from_pad]
    r_s = r_s[switch_from_pad]

    one_hot_labels = F.one_hot(targets.to(torch.long), num_classes=num_classes).float()

    if len(b_index):
        for i, prob in enumerate(inter_probs):

            idex = torch.tensor(len(inter_probs)-i)
            assert(b_index.max() <= lengths.size(0)), f'{b_index.max()} > bs {lengths.size(0)}'
            assert((l_index+half_way-idex).max() < one_hot_labels.size(-2)), f'{(l_index+half_way-idex).max()} > max seq len'

            one_hot_labels[b_index, l_index+half_way-idex, l_s] = prob[1]
            one_hot_labels[b_index, l_index+half_way-idex, r_s] = prob[0]
            
    del r_s, l_s, l_index, r_index, inter_probs

    return one_hot_labels.float()

# Modified http://nlp.seas.harvard.edu/annotated-transformer/#optimizer
def rate(step, max_lr, factor, warmup):
    """
    we have to default the step to 1 for LambdaLR function
    to avoid zero raising to negative power.
    """
    model_size = (max_lr / (warmup * warmup ** (-1.5)))**-2
    if step == 0:
        step = 1
    return factor * (
        model_size ** (-0.5) * min(step ** (-0.5), step * warmup ** (-1.5))
    )

# Based off Deep Learning, Goodfellow et al definition
class CustomLabelSmoothingCrossEntropy(nn.Module):
    def __init__(self, epsilon: float = 0.1, reduction='mean'):
        """Custom cross entropy with label smoothing, which allows
        for label smoothing to be applied to a ground truth distribution
        IMPORTANT: Does not get same loss as torch w/ one-hot labels """
        super().__init__()
        self.epsilon = epsilon
        self.reduction = reduction

    def forward(self, preds, target):
        if len(target.size()) < 2: F.one_hot(target)
        n = target.size(-1)

        max_probs, idxs = torch.max(target, dim=-1)
        
        residuals = max_probs * self.epsilon
        norm_residuals = residuals / n

        target[range(len(idxs)), idxs] = target[range(len(idxs)), idxs] - residuals
        target = target + norm_residuals.unsqueeze(dim=-1).repeat(1, n)
        # Commenting this out replicates troch's implementation
        # target[range(len(idxs)), idxs] = target[range(len(idxs)), idxs] - norm_residuals
        
        return F.cross_entropy(preds, target)