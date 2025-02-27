import argparse
import datetime
import logging
import os
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn, optim

import sys
sys.path.insert(1, './')

from debias.datasets.cifar10 import get_cifar10
from debias.networks.resnet_cifar import ResNet18
from debias.losses.groupDro import LossComputer
from debias.utils.logging import set_logging
from debias.utils.utils import (AverageMeter, MultiDimAverageMeter, accuracy,
                                save_model, set_seed, pretty_dict)

from tqdm import tqdm

def parse_option():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exp_name', type=str, default='test', )
    parser.add_argument('--gpu', type=int, default=0)

    parser.add_argument('--print_freq', type=int, default=300,
                        help='print frequency')
    parser.add_argument('--save_freq', type=int, default=200,
                        help='save frequency')
    parser.add_argument('--epochs', type=int, default=200,
                        help='number of training epochs')
    parser.add_argument('--seed', type=int, default=1)

    parser.add_argument('--bs', type=int, default=128, help='batch_size')
    parser.add_argument('--lr', type=float, default=0.1)
    parser.add_argument('--corr', type=float, default=0.95)

    parser.add_argument('--robust', default=False, action='store_true')
    parser.add_argument('--alpha', type=float, default=0.2)
    parser.add_argument('--generalization_adjustment', default="0.0")
    parser.add_argument('--automatic_adjustment', default=False, action='store_true')
    parser.add_argument('--robust_step_size', default=0.01, type=float)
    parser.add_argument('--use_normalized_loss', default=False, action='store_true')
    parser.add_argument('--btl', default=False, action='store_true')
    parser.add_argument('--gamma', type=float, default=0.1)
    parser.add_argument('--minimum_variational_weight', type=float, default=0)
    parser.add_argument('--reweight_sampler', default=False,  action='store_true')
    parser.add_argument('--weight_decay', type=float, default=1e-4)

    opt = parser.parse_args()

    return opt


def set_model(opt, dataset):
    model = ResNet18(num_classes=10).cuda()
    adjustments = [float(c) for c in opt.generalization_adjustment.split(',')]
    if len(adjustments)==1:
        adjustments = np.array(adjustments* dataset.n_groups)
    else:
        adjustments = np.array(adjustments)


    criterion = LossComputer(
        torch.nn.CrossEntropyLoss(reduction='none'),
        is_robust=opt.robust,
        dataset=dataset,
        alpha=opt.alpha,
        gamma=opt.gamma,
        adj=adjustments,
        step_size=opt.robust_step_size,
        normalize_loss=opt.use_normalized_loss,
        btl=opt.btl,
        min_var_weight=opt.minimum_variational_weight)

    return model, criterion

def count_parameters(model):
    from prettytable import PrettyTable
    table = PrettyTable(["Modules", "Parameters"])
    total_params = 0
    for name, parameter in model.named_parameters():
        if not parameter.requires_grad: continue
        params = parameter.numel()
        table.add_row([name, params])
        total_params+=params
    print(table)
    print(f"Total Trainable Params: {total_params}")
    return total_params


def train(train_loader, model, criterion, optimizer):
    model.train()
    avg_loss = AverageMeter()

    train_iter = iter(train_loader)

    for images, labels, _, biases, _, group_idx in tqdm(train_iter, ascii=True):
        bsz = labels.shape[0]
        labels, biases = labels.cuda(), biases.cuda()
        group_idx = group_idx.cuda()

        images = images.cuda()
        logits, _ = model(images)       

        loss = criterion.loss(logits, labels, group_idx, True)

        avg_loss.update(loss.item(), bsz)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    return avg_loss.avg


def validate(val_loader, model):
    model.eval()

    top1 = AverageMeter()
    attrwise_acc_meter = MultiDimAverageMeter(dims=(10, 2))

    with torch.no_grad():
        for images, labels, _, biases, _, _ in val_loader:
            images, labels, biases = images.cuda(), labels.cuda(), biases.cuda()
            bsz = labels.shape[0]

            output, _ = model(images)
            preds = output.data.max(1, keepdim=True)[1].squeeze(1)

            acc1, = accuracy(output, labels, topk=(1,))
            top1.update(acc1[0], bsz)

            corrects = (preds == labels).long()
            attrwise_acc_meter.add(corrects.cpu(), torch.stack([labels.cpu(), biases.cpu()], dim=1))
    

    bc_classes = [1]*10 
    for i in [0, 2, 4, 6, 8]: 
        bc_classes[i] = 0

    return top1.avg, attrwise_acc_meter.get_unbiased_acc(), attrwise_acc_meter.get_acc_diff(), attrwise_acc_meter.get_bias_conflict(bc_classes)


def main():
    opt = parse_option()

    exp_name = f'dro-cifar10-{opt.exp_name}-lr{opt.lr}-bs{opt.bs}-seed{opt.seed}'
    opt.exp_name = exp_name

    output_dir = f'exp_results/{exp_name}'
    save_path = Path(output_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    set_logging(exp_name, 'INFO', str(save_path))
    set_seed(opt.seed)
    logging.info(f'save_path: {save_path}')

    np.set_printoptions(precision=3)
    torch.set_printoptions(precision=3)

    root = './data/cifar10'
    train_loader = get_cifar10(
        root,
        split='train',
        aug=False, 
        corr=opt.corr)

    val_loaders = {}
    val_loaders['valid'] = get_cifar10(
        root,
        split='valid', 
        aug=False,
        corr=opt.corr
    )
    val_loaders['test'] = get_cifar10(
        root,
        split='test', 
        aug=False)

    model, criterion = set_model(opt, train_loader.dataset)

    decay_epochs = [50, 100, 150]

    optimizer = optim.SGD(model.parameters(), lr=opt.lr, momentum =0.9, weight_decay=opt.weight_decay)
    scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=decay_epochs, gamma=0.1)
    logging.info(f"decay_epochs: {decay_epochs}")

    #(save_path / 'checkpoints').mkdir(parents=True, exist_ok=True)

    best_accs = {'valid': 0, 'test': 0}
    best_epochs = {'valid': 0, 'test': 0}
    best_stats = {}
    start_time = time.time()
    for epoch in range(1, opt.epochs + 1):
        logging.info(f'[{epoch} / {opt.epochs}] Learning rate: {scheduler.get_last_lr()[0]}')
        loss = train(train_loader, model, criterion, optimizer)
        logging.info(f'[{epoch} / {opt.epochs}] Loss: {loss}')

        scheduler.step()

        stats = pretty_dict(epoch=epoch)
        for key, val_loader in val_loaders.items():
            _, acc_unbiased, diff, bias_conflict = validate(val_loader, model)
            stats[f'{key}/acc_unbiased'] = acc_unbiased.item() * 100
            stats[f'{key}/diff'] = diff.item() * 100
            stats[f'{key}/bias_conflict'] = bias_conflict.item() * 100

        logging.info(f'[{epoch} / {opt.epochs}] {stats}')
        for tag in best_accs.keys():
            if stats[f'{tag}/bias_conflict'] > best_accs[tag]:
                best_accs[tag] = stats[f'{tag}/bias_conflict']
                best_epochs[tag] = epoch
                best_stats[tag] = pretty_dict(**{f'best_{tag}_{k}': v for k, v in stats.items()})

                #save_file = save_path / 'checkpoints' / f'best_{tag}.pth'
                # save_model(model, optimizer, opt, epoch, save_file)
            logging.info(
                f'[{epoch} / {opt.epochs}] best {tag} accuracy: {best_accs[tag]:.3f} at epoch {best_epochs[tag]} \n best_stats: {best_stats[tag]}')

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    logging.info(f'Total training time: {total_time_str}')

    #save_file = save_path / 'checkpoints' / f'last.pth'
    # save_model(model, optimizer, opt, opt.epochs, save_file)


if __name__ == '__main__':
    main()
