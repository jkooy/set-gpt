import torch
import torch.nn as nn
from torch.utils.data import TensorDataset, DataLoader, random_split
import pytorch_lightning as pl
import numpy as np
import logging
from argparse import ArgumentParser
import logging
import os

from gpt2 import GPT2

def get_logger(file_path):
    """ Make python logger """
    # [!] Since tensorboardX use default logger (e.g. logging.info()), we should use custom logger
    logger = logging.getLogger('/set')
    log_format = '%(asctime)s | %(message)s'
    formatter = logging.Formatter(log_format, datefmt='%m/%d %I:%M:%S %p')
    file_handler = logging.FileHandler(file_path)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.setLevel(logging.INFO)

    return logger

logger = get_logger(os.path.join('', "{}.log".format(set)))


def _shape_input(x):
    """shape batch of images for input into GPT2 model"""
    x = x.view(x.shape[0], -1)  # flatten images into sequences
    x = x.transpose(0, 1).contiguous()  # to shape [seq len, batch]
    return x


class ImageGPT(pl.LightningModule):
    def __init__(self, hparams):
        super(ImageGPT, self).__init__()
        self.hparams = hparams
        self.gpt = GPT2(
            embed_dim=self.hparams.embed_dim,
            num_heads=self.hparams.num_heads,
            num_layers=self.hparams.num_layers,
            num_positions=self.hparams.num_pixels * self.hparams.num_pixels,
            num_vocab=self.hparams.num_vocab,
            num_classes=self.hparams.num_classes,
        )

        self.criterion = nn.CrossEntropyLoss()
        
    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument("--embed_dim", type=int, default=16)
        parser.add_argument("--num_heads", type=int, default=2)
        parser.add_argument("--num_layers", type=int, default=8)
        parser.add_argument("--num_pixels", type=int, default=28)
        parser.add_argument("--num_vocab", type=int, default=16)
        parser.add_argument("--num_classes", type=int, default=10)
        parser.add_argument("--classify", action="store_true", default=False)
        parser.add_argument("--batch_size", type=int, default=64)
        parser.add_argument("--learning_rate", type=float, default=1e-2)
        parser.add_argument("--steps", type=int, default=25_000)
        return parser

    def configure_optimizers(self):
        optim = torch.optim.Adam(self.gpt.parameters(), lr=self.hparams.learning_rate)

        # paper states cosine annealing is only used for pretraining
        if self.hparams.classify:
            return optim

        sched = torch.optim.lr_scheduler.CosineAnnealingLR(optim, self.hparams.steps)
        return [optim], [sched]

    def forward(self, x):
        return self.gpt(x)

    def training_step(self, batch, batch_idx):
        x, y = batch
#         logger.info("reshape train x,y size is {}{}".format(np.shape(x), np.shape(y))) 
        x = _shape_input(x)
        
#         print('x shape in training is:', x.size(), 'y shape in training is:', y.size())
        #####（784，64） （64）
        if self.hparams.classify:
            clf_logits = self.gpt(x, classify=True)
            loss = self.criterion(clf_logits, y)
        else:
            logits = self.gpt(x)
            logger.info("reshape train y size is {}".format(np.shape(logits.view(-1, logits.size(-1)))))          
            logger.info("reshape train x size is {}".format(np.shape(x.view(-1))))
            loss = self.criterion(logits.view(-1, logits.size(-1)), x.view(-1))

        logs = {"loss": loss}
        return {"loss": loss, "log": logs}

    def validation_step(self, batch, batch_idx):
        x, y = batch
        x = _shape_input(x)

        if self.hparams.classify:
            clf_logits = self.gpt(x, classify=True)
            loss = self.criterion(clf_logits, y)
            _, preds = torch.max(clf_logits, 1)
            correct = preds == y
            return {"val_loss": loss, "correct": correct}
        else:
            logits = self.gpt(x)
            loss = self.criterion(logits.view(-1, logits.size(-1)), x.view(-1))
            return {"val_loss": loss}

    def validation_epoch_end(self, outs):
        avg_loss = torch.stack([x["val_loss"] for x in outs]).mean()
        logs = {"val_loss": avg_loss}
        if self.hparams.classify:
            correct = torch.cat([x["correct"] for x in outs])
            logs["val_acc"] = correct.sum().float() / correct.shape[0]
        return {"val_loss": avg_loss, "log": logs}

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def test_epoch_end(self, outs):
        result = self.validation_epoch_end(outs)

        # replace valid stats with test stats becuase we are reusing function
        result["log"]["test_loss"] = result["log"].pop("val_loss")
        result["test_loss"] = result.pop("val_loss")
        if self.hparams.classify:
            result["log"]["test_acc"] = result["log"].pop("val_acc")
        return result

    def prepare_data(self):
        ds = lambda x, y: TensorDataset(torch.from_numpy(x), torch.from_numpy(y))

        train_x = np.load(self.hparams.train_x)
#         logger.info("train x size is {}".format(np.shape(train_x)))
        train_y = np.load(self.hparams.train_y)
#         logger.info("train y size is {}".format(np.shape(train_x)))
        test_x = np.load(self.hparams.test_x)
#         logger.info("test x size is {}".format(np.shape(train_x)))
        test_y = np.load(self.hparams.test_y)
#         logger.info("test y size is {}".format(np.shape(train_x)))

        train_ds = ds(train_x, train_y)
        train_size = int(0.9 * len(train_ds))
        self.train_ds, self.valid_ds = random_split(
            train_ds, [train_size, len(train_ds) - train_size]
        )

        self.test_ds = ds(test_x, test_y)

    def train_dataloader(self):
        return DataLoader(
            self.train_ds,
            shuffle=True,
            batch_size=self.hparams.batch_size,
            num_workers=4,
        )

    def val_dataloader(self):
        return DataLoader(
            self.valid_ds, batch_size=self.hparams.batch_size, num_workers=4
        )

    def test_dataloader(self):
        return DataLoader(
            self.test_ds, batch_size=self.hparams.batch_size, num_workers=4
        )
