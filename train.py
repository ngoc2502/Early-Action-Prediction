from config.config import get_cfg_defaults
from model import generate_model
import torch
import torch.nn.functional as F
import torch.nn as nn
from einops import rearrange, repeat
from data_loader import iterator_factory_augmentResNet
from loss import create_loss
from loss import metric
from datetime import datetime
import os

class Combine(nn.Module):

    def __init__(self, cfg):
        super().__init__()
        self.backbone = generate_model(cfg.BACKBONE)

        # for param in self.backbone.parameters():
        #     param.requires_grad = False

        self.head = generate_model(cfg.HEAD)

        self.file_name = cfg.TRAIN.MODEL_NAME

    def forward(self, data):

        B, *_ = data.shape

        data = rearrange(data, 'b s c t h w -> (b s) c t h w')

        # feature: ((b s), c, t, h, w)
        # pred: ((b s), n)
        features, pred = self.backbone(data)

        features = rearrange(features, '(b s) c t h w -> b s c t h w', b = B)

        # features: (b, s, c, t, h, w)
        outputs = self.head(features)
        return outputs

    def save_model(self, file_path, epoch):
        file_name = f"{file_path}-epoch:{epoch}.pth"
        torch.save(self.state_dict(), file_name)
        pass

    def load_model(self, file_path):
        result = torch.load(file_path)
        pass

def load_checkpoint(cfg ,model, optimizer):

    checkpoint = torch.load(cfg.TRAIN.PRETRAIN_PATH)
    model.load_state_dict(checkpoint['model_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    epoch = checkpoint['epoch']
    loss = checkpoint['loss']

    return epoch

def save_checkpoint(cfg, model, optimizer, epoch, loss):

    file_path = f"{cfg.TRAIN.MODEL_NAME}-epoch:{epoch}.pth"

    torch.save({
            'epoch': epoch,
            'model_state_dict': model.state_dict(),
            'optimizer_state_dict': optimizer.state_dict(),
            'loss': loss,
            }, file_path)

def create_file_log(cfg):
    """
    Update file dir, file train log, file val log, model name
    """
    now = datetime.now().strftime("%y_%m_%d")
    file_dir = f"{now}-{cfg.BACKBONE.NAME}-{cfg.HEAD.NAME}-video_per:{cfg.DATA.VIDEO_PER}-num_samplers:{cfg.DATA.NUM_SAMPLERS}-optimize:{cfg.TRAIN.OPTIMIZER}-loss:{cfg.TRAIN.LOSS}"
    file_dir = os.path.join(cfg.TRAIN.RESULT_DIR, file_dir)

    if not os.path.exists(file_dir):
        os.makedirs(file_dir)

    cfg.TRAIN.RESULT_DIR = file_dir
    cfg.TRAIN.LOG_FILE_TRAIN = file_dir + '/train.csv'
    cfg.TRAIN.LOG_FILE_VAL = file_dir + '/val.csv'
    cfg.TRAIN.MODEL_NAME = file_dir + f"/{cfg.BACKBONE.NAME}-{cfg.HEAD.NAME}-video_per:{cfg.DATA.VIDEO_PER}-num_samplers:{cfg.DATA.NUM_SAMPLERS}-optimize:{cfg.TRAIN.OPTIMIZER}-loss:{cfg.TRAIN.LOSS}"
    
    # save config file
    with open(file_dir + '/config.yaml', 'w') as f:
        f.write(cfg.dump())
    pass

def create_optimizer(model, cfg):

    params = {
        'classifier':{'lr':cfg.TRAIN.LR_MULT[2],
                'params':[]},
        'head':{'lr':cfg.TRAIN.LR_MULT[0],
                'params':[]},
        'pool':{'lr':cfg.TRAIN.LR_MULT[1],
                'params':[]},
        'base':{'lr':cfg.TRAIN.LEARNING_RATE,
                'params':[]}
    }

    # Iterate over all parameters
    for name, param in model.named_parameters():
        if 'fc' in name.lower():
            params['classifier']['params'].append(param)
        elif 'head' in name.lower():
            params['head']['params'].append(param)
        elif 'pred_fusion' in name.lower():
            params['pool']['params'].append(param)
        params['base']['params'].append(param)

    optim = torch.optim.Adam([
            {'params': params['classifier']['params'], 'lr_mult': params['classifier']['lr']},
            {'params': params['head']['params'], 'lr_mult': params['head']['lr']},
            {'params': params['pool']['params'], 'lr_mult': params['pool']['lr']},],
            lr = cfg.TRAIN.LEARNING_RATE,
            weight_decay = cfg.TRAIN.WEIGHT_DECAY)
    
    return optim

def adjust_learning_rate(lr, optimiser):
        # learning rate adjustment based on provided lr rate
        for param_group in optimiser.param_groups:
            if 'lr_mult' in param_group:
                lr_mult = param_group['lr_mult']
            else:
                lr_mult = 1.0
            param_group['lr'] = lr * lr_mult

if __name__ == '__main__':
    cfg = get_cfg_defaults()

    # merge with .yaml

    # create file log path
    create_file_log(cfg)

    # input is the config in log dicrectory of model pretrain
    if cfg.TRAIN.TRAIN_CHECKPOINT == True:
        cfg.merge_from_file()
    
    
    cfg.freeze()

    model = Combine(cfg).to(cfg.TRAIN.DEVICE)

    # create data loader
    train_loader, val_loader, len_video_train, len_video_val = iterator_factory_augmentResNet.create(cfg.DATA)
    # val_loader, len_video_val = iterator_factory_temp.create(cfg.DATA)

    criterion = create_loss.CrossEntropyLoss
    optim = create_optimizer(model, cfg)

    train_metric = metric.MyMetric(cfg, file_path = cfg.TRAIN.LOG_FILE_TRAIN, num_iter = len(train_loader))
    val_metric = metric.MyMetric(cfg, file_path = cfg.TRAIN.LOG_FILE_VAL, num_iter = len(val_loader))

    start_epoch = 0
    if cfg.TRAIN.TRAIN_CHECKPOINT == True:
        start_epoch = load_checkpoint(cfg, model, optim) + 1
        pass

    # ---------- Code train AI --------------
    for epoch in range(start_epoch, cfg.TRAIN.EPOCH):

        train_metric.reset_epoch()
        val_metric.reset_epoch()
        # train
        for i, (data, targets, video_path) in enumerate(train_loader):
            # import numpy as np
            # video = (data[0, 0, ...]*255).detach().cpu().numpy().transpose(1,2,3,0).astype(np.uint8)

            # reset metric
            train_metric.reset_batch()

            # data shape (B, S, C, T, H, W)
            data = data.cuda()
            targets = targets.cuda()

            # zero the parameter gradients
            optim.zero_grad()
            outputs = model(data)
            # outputs = rearrange(outputs, '(b s) c -> b s c', b = cfg.TRAIN.BATCH_SIZE)
            # loss = 0
            loss = criterion(outputs, targets)
            loss.backward()

            # temp here, we will apply multi grid training
            adjust_learning_rate(0.01, optim)
            optim.step()

            # calculate metric in here
            train_metric.update(outputs, targets, loss)
            # print log
            if i != len(train_loader) - 1:
                train_metric.logg(i, epoch = epoch)
            else:
                train_metric.logg(i, True, epoch)


        # end epoch
        if epoch%cfg.TRAIN.SAVE_FREQUENCY == 0 or epoch == cfg.TRAIN.EPOCH - 1:
            save_checkpoint(cfg, model, optim, epoch, loss)
        # write in file
        train_metric.write_file(epoch)

        # evaluation
        with torch.no_grad():

            accuracy_val = 0
            loss_val = 0

            for i, (data, targets, video_path) in enumerate(val_loader):
                val_metric.reset_batch()
                if i == len(val_loader) - 1:
                    hihi = 0
                data = data.cuda()
                targets = targets.cuda()
                # zero the parameter gradients
                outputs = model(data)
                # outputs = rearrange(outputs, '(b s) c -> b s c', s = cfg.DATA.NUM_SAMPLERS)
                loss = criterion(outputs, targets)

                # calculate metrix here
                val_metric.update(outputs, targets, loss)

                # print log
                if i != len(val_loader) - 1:
                    val_metric.logg(i, epoch = epoch)
                else:
                    val_metric.logg(i, True, epoch)


            # write in file
            val_metric.write_file(epoch)


        
