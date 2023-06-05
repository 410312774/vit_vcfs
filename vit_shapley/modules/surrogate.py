import logging

import pytorch_lightning as pl
import torch
import torch.nn as nn
from torchvision import models as cnn_models

import vit_shapley.modules.vision_transformer as vit
from vit_shapley.modules import surrogate_utils


class Surrogate(pl.LightningModule):
    """
    `pytorch_lightning` module for surrogate

    Args:
        mask_location: how the mask is applied to the input. ("pre-softmax" or "zero-input")
        backbone_type: should be the class name defined in `torchvision.models.cnn_models` or `timm.models.vision_transformer`
        download_weight: whether to initialize backbone with the pretrained weights
        load_path: If not None. loads the weights saved in the checkpoint to the model
        target_type: `binary` or `multi-class` or `multi-label`
        output_dim: the dimension of output
        target_model: This model will be trained to generate output similar to the output generated by 'target_model' for the same input.
        checkpoint_metric: the metric used to determine whether to save the current status as checkpoints during the validation phase
        optim_type: type of optimizer for optimizing parameters
        learning_rate: learning rate of optimizer
        weight_decay: weight decay of optimizer
        decay_power: only `cosine` annealing scheduler is supported currently
        warmup_steps: parameter for the `cosine` annealing scheduler
    """

    def __init__(self, mask_location: str, backbone_type: str, download_weight: bool, load_path: str or None,
                 target_type: str, output_dim: int,

                 target_model: pl.LightningModule or nn.Module or None, checkpoint_metric: str or None,
                 optim_type: str or None,
                 learning_rate: float or None, weight_decay: float or None,
                 decay_power: str or None, warmup_steps: int or None, load_path_state_dict=False):

        super().__init__()
        self.save_hyperparameters()

        self.logger_ = logging.getLogger(__name__)

        assert not (self.hparams.download_weight and self.hparams.load_path is not None), \
            "'download_weight' and 'load_path' cannot be activated at the same time as the downloaded weight will be overwritten by weights in 'load_path'."

        # Backbone initialization. (currently support only vit and cnn)
        if hasattr(vit, self.hparams.backbone_type):
            self.backbone = getattr(vit, self.hparams.backbone_type)(pretrained=self.hparams.download_weight)
        elif hasattr(cnn_models, self.hparams.backbone_type):
            self.backbone = getattr(cnn_models, self.hparams.backbone_type)(pretrained=self.hparams.download_weight)
        else:
            raise NotImplementedError("Not supported backbone type")
        if self.hparams.download_weight:
            self.logger_.info("The backbone parameters were initialized with the downloaded pretrained weights.")
        else:
            self.logger_.info("The backbone parameters were randomly initialized.")

        # Nullify classification head built in the backbone module and rebuild.
        if self.backbone.__class__.__name__ == 'VisionTransformer':
            head_in_features = self.backbone.head.in_features
            self.backbone.head = nn.Identity()
        elif self.backbone.__class__.__name__ == 'ResNet':
            head_in_features = self.backbone.fc.in_features
            self.backbone.fc = nn.Identity()
        elif self.backbone.__class__.__name__ == 'DenseNet':
            head_in_features = self.backbone.classifier.in_features
            self.backbone.classifier = nn.Identity()
        else:
            raise NotImplementedError("Not supported backbone type")
        self.head = nn.Linear(head_in_features, self.hparams.output_dim)

        # Load checkpoints
        if self.hparams.load_path is not None:
            if load_path_state_dict:
                state_dict = torch.load(self.hparams.load_path, map_location="cpu")
            else:
                checkpoint = torch.load(self.hparams.load_path, map_location="cpu")
                state_dict = checkpoint["state_dict"]               
            ret = self.load_state_dict(state_dict, strict=False)
            self.logger_.info(f"Model parameters were updated from a checkpoint file {self.hparams.load_path}")
            self.logger_.info(f"Unmatched parameters - missing_keys:    {ret.missing_keys}")
            self.logger_.info(f"Unmatched parameters - unexpected_keys: {ret.unexpected_keys}")

        # Check the validity of 'mask_location` parameter
        if hasattr(vit, self.hparams.backbone_type):
            assert self.hparams.mask_location in ["pre-softmax", "post-softmax",
                                                  "zero-input", "zero-embedding",
                                                  "random-sampling"], f"'mask_location' for ViT models must be 'pre-softmax', 'post-softmax', 'zero-input', 'zero-embedding', or 'random-sampling', but {self.hparams.mask_location}"
        elif hasattr(cnn_models, self.hparams.backbone_type):
            assert self.hparams.mask_location == "zero-input", "'mask_location' for CNN models must be 'zero-input'"
        else:
            raise NotImplementedError("Not supported backbone type")
        # Set `num_players` variable.
        if hasattr(self.backbone, 'patch_embed'):
            self.num_players = self.backbone.patch_embed.num_patches
        else:
            self.num_players = 196

        # Set up modules for calculating metric
        surrogate_utils.set_metrics(self)

    def configure_optimizers(self):
        return surrogate_utils.set_schedule(self)

    def forward(self, images, masks, mask_location=None):
        assert masks.shape[-1] == self.num_players
        mask_location = self.hparams.mask_location if mask_location is None else mask_location

        if self.backbone.__class__.__name__ == 'VisionTransformer':
            if mask_location in ['pre-softmax', 'post-softmax', 'zero-input', 'zero-embedding', 'random-sampling']:
                output = self.backbone(x=images, mask=masks, mask_location=mask_location)
                embedding_cls, embedding_tokens = output['x'], output['x_others']
                logits = self.head(embedding_cls)
                output.update({'logits': logits})
            else:
                raise ValueError(
                    "'mask_location' should be 'pre-softmax', 'post-softmax', 'zero-out', 'zero-embedding', 'random-sampling'")
        elif self.backbone.__class__.__name__ == 'ResNet':
            if mask_location == 'zero-input':
                if images.shape[2:4] == (224, 224) and masks.shape[1] == 196:
                    masks = masks.reshape(-1, 14, 14)
                    masks = torch.repeat_interleave(torch.repeat_interleave(masks, 16, dim=2), 16, dim=1)
                else:
                    raise NotImplementedError
                images_masked = images * masks.unsqueeze(1)
                out = self.backbone(images_masked)
                logits = self.head(out)
                output = {'logits': logits}
            else:
                raise ValueError("'mask_location' should be 'zero-out'")
        else:
            raise NotImplementedError("Not supported backbone type")

        return output

    def training_step(self, batch, batch_idx):
        images, masks = batch["images"], batch["masks"]
        logits = self(images, masks)['logits']
        self.hparams.target_model.eval()
        with torch.no_grad():
            logits_target = self.hparams.target_model(images.to(self.hparams.target_model.device))['logits'].to(
                self.device)
        loss = surrogate_utils.compute_metrics(self, logits=logits, logits_target=logits_target, phase='train')
        return loss

    def training_epoch_end(self, outs):
        surrogate_utils.epoch_wrapup(self, phase='train')

    def validation_step(self, batch, batch_idx):
        images, masks = batch["images"], batch["masks"]
        logits = self(images, masks)['logits']
        self.hparams.target_model.eval()
        with torch.no_grad():
            logits_target = self.hparams.target_model(images.to(self.hparams.target_model.device))['logits'].to(
                self.device)
        loss = surrogate_utils.compute_metrics(self, logits=logits, logits_target=logits_target, phase='val')

    def validation_epoch_end(self, outs):
        surrogate_utils.epoch_wrapup(self, phase='val')

    def test_step(self, batch, batch_idx):
        images, masks = batch["images"], batch["masks"]
        logits = self(images, masks)['logits']
        self.hparams.target_model.eval()
        with torch.no_grad():
            logits_target = self.hparams.target_model(images.to(self.hparams.target_model.device))['logits'].to(
                self.device)
        loss = surrogate_utils.compute_metrics(self, logits=logits, logits_target=logits_target, phase='test')

    def test_epoch_end(self, outs):
        surrogate_utils.epoch_wrapup(self, phase='test')
