import copy
import sys
import time
from argparse import Namespace
from typing import Tuple

import matplotlib.pyplot as plt
# import megengine as mge
import numpy as np
import seaborn as sns
import torch
import torch.distributed as dist
from apex import amp
from apex.parallel import DistributedDataParallel, convert_syncbn_model
from dataset import get_dataset
from dataset.utils.continual_dataset import ContinualDataset
from models.utils.continual_model import ContinualModel
from sklearn import datasets
from sklearn.manifold import TSNE
# from torchsummaryX import summary
# from ptflops import get_model_complexity_info
from torch import nn

from utils.loggers import *
from utils.loggers import CsvLogger
from utils.status import create_stash, progress_bar
from utils.tb_logger import *

torch.manual_seed(0)
np.random.seed(0)

torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = True

def mask_classes(outputs: torch.Tensor, dataset: ContinualDataset, k: int) -> None:
    """
    Given the output tensor, the dataset at hand and the current task,
    masks the former by setting the responses for the other tasks at -inf.
    It is used to obtain the results for the task-il setting.
    :param outputs: the output tensor
    :param dataset: the continual dataset
    :param k: the task index
    """

    if dataset.NAME == 'seq-core50':
        N_CLASSES_PER_TASK = [10, 5, 5, 5, 5, 5, 5, 5, 5]
        FROM_CLASS = int(np.sum(N_CLASSES_PER_TASK[:k]))
        TO_CLASS = int(np.sum(N_CLASSES_PER_TASK[:k+1]))
        outputs[:, 0:FROM_CLASS] = -float('inf')
        outputs[:, TO_CLASS: 50] = -float('inf')
    else:
        outputs[:, 0:k * dataset.N_CLASSES_PER_TASK] = -float('inf')
        outputs[:, (k + 1) * dataset.N_CLASSES_PER_TASK:
                   dataset.N_TASKS * dataset.N_CLASSES_PER_TASK] = -float('inf')


def evaluate(model: ContinualModel, dataset: ContinualDataset, task_id, last=False) -> Tuple[list, list]:
    """
    Evaluates the accuracy of the model for each past task.
    :param model: the model to be evaluated
    :param dataset: the continual dataset at hand
    :return: a tuple of lists, containing the class-il
             and task-il accuracy for each task
    """
    status = model.net.training
    model.net.eval()
    gamma = 1
    accs, accs_mask_classes = [], []
    for k, test_loader in enumerate(dataset.test_loaders):

        correct, correct_mask_classes, total = 0.0, 0.0, 0.0
        for data in test_loader:
            inputs, labels = data
            inputs, labels = inputs.to(model.device, non_blocking=True), labels.to(model.device, non_blocking=True)
            with torch.no_grad():
                if 'class-il' not in model.COMPATIBILITY:
                    outputs = model(inputs, k)
                elif last:
                    outputs = model.old_means_pre(inputs)
                elif dataset.args.model == 'our':
                    outputs = model(inputs, dataset)
                else:
                    if (dataset.args.model == 'derppcct' or dataset.args.model == 'onlinevt') and model.net.net.distill_classifier:
                        outputs = model.net.net.distill_classification(inputs)
                        # outputs = model.ncm(inputs)
                        outputs[:, (task_id) * dataset.N_CLASSES_PER_TASK: (task_id) * dataset.N_CLASSES_PER_TASK+ dataset.N_CLASSES_PER_TASK] \
                            = outputs[:, (task_id) * dataset.N_CLASSES_PER_TASK: (task_id) * dataset.N_CLASSES_PER_TASK+ dataset.N_CLASSES_PER_TASK] * gamma
                    else:
                        outputs = model(inputs)

                _, pred = torch.max(outputs.data, 1)
                correct += torch.sum(pred == labels).item()
                total += labels.shape[0]

                if dataset.SETTING == 'class-il':
                    mask_classes(outputs, dataset, k)
                    _, pred = torch.max(outputs.data, 1)
                    correct_mask_classes += torch.sum(pred == labels).item()

        accs.append(round(correct / total * 100, 3)
                    if 'class-il' in model.COMPATIBILITY else 0)
        accs_mask_classes.append(round(correct_mask_classes / total * 100, 3))

    model.net.train(status)
    return accs, accs_mask_classes


def train(model: ContinualModel, dataset: ContinualDataset,
          args: Namespace) -> None:
    """
    The training process, including evaluations and loggers.
    :param model: the module to be trained
    :param dataset: the continual dataset at hand
    :param args: the arguments of the current execution
    """

    model_stash = create_stash(model, args, dataset)
    results, results_mask_classes = [], []

    if args.csv_log:
        csv_logger = CsvLogger(dataset.SETTING, dataset.NAME, model.NAME, dataset.N_TASKS)
    if args.tensorboard:
        tb_logger = TensorboardLogger(args, dataset.SETTING, model_stash)
        model_stash['tensorboard_name'] = tb_logger.get_name()

    if model.NAME != 'icarl' and model.NAME != 'pnn' and model.NAME != 'our' and model.NAME != 'our_reservoir' and model.NAME !='er_tricks' and model.NAME !='onlinevt':
        dataset_copy = get_dataset(args)
        for t in range(dataset.N_TASKS):
            model.net.train()
            _, _ = dataset_copy.get_data_loaders()
        random_results_class, random_results_task = evaluate(model, dataset_copy, 0)

    start = time.time()
    print(file=sys.stderr)

    if hasattr(model.args, 'ce'):
        ce = model.args.ce
        model.args.ce = 1
    for t in range(dataset.N_TASKS):
        model.net.train()
        if args.use_lr_scheduler:
            model.set_opt()
        train_loader, test_loader = dataset.get_data_loaders()
        if hasattr(model, 'begin_task'):
            if model.NAME != 'our':
                model.begin_task(dataset)
            elif args.begin_task:
                model.begin_task(dataset)
        if hasattr(model.args, 'ce'):
            if t > 0:
                model.args.ce = ce * model.args.ce
                pass
            print('model.args.ce: ', model.args.ce)
        for epoch in range(args.n_epochs - int(t*0)):
            if args.use_distributed:
                train_loader.sampler.set_epoch(epoch)
                test_loader.sampler.set_epoch(epoch)

            model.display_img = False

            for i, data in enumerate(train_loader):
                if i == (train_loader.__len__()-1):
                    model.display_img = True
                if hasattr(dataset.train_loader.dataset, 'logits'):
                    inputs, labels, not_aug_inputs, logits = data
                    inputs = inputs.to(model.device, non_blocking=True)
                    labels = labels.to(model.device, non_blocking=True)
                    not_aug_inputs = not_aug_inputs.to(model.device, non_blocking=True)
                    logits = logits.to(model.device, non_blocking=True)
                    loss = model.observe(inputs, labels, not_aug_inputs, logits)
                else:
                    inputs, labels, not_aug_inputs = data
                    inputs, labels = inputs.to(model.device, non_blocking=True), labels.to(
                        model.device, non_blocking=True)
                    not_aug_inputs = not_aug_inputs.to(model.device)
                    loss = model.observe(inputs, labels, not_aug_inputs)

                # progress_bar(i, len(train_loader), epoch, t, loss)

                if args.tensorboard:
                    tb_logger.log_loss(loss, args, epoch, t, i)

                if hasattr(model, 'middle_task') and (i % 2000) == 0 and i > 0 and dataset.NAME == 'seq-mnist':
                    tmp_buffer = copy.deepcopy(model.buffer)
                    model.middle_task(dataset)
                    accs = evaluate(model, dataset, t)
                    model.buffer = tmp_buffer
                    print(accs)
                    mean_acc = np.mean(accs, axis=1)
                    print_mean_accuracy(mean_acc, t + 1, dataset.SETTING)

                model_stash['batch_idx'] = i + 1
            model_stash['epoch_idx'] = epoch + 1
            model_stash['batch_idx'] = 0
            if model._scheduler is not None:
                model._scheduler.step()
        model_stash['task_idx'] = t + 1
        model_stash['epoch_idx'] = 0

        if hasattr(model, 'end_task'):
            if model.NAME == 'our':
                model.end_task(dataset, t)
            else:
                model.end_task(dataset)

        accs = evaluate(model, dataset, t)
        results.append(accs[0])
        results_mask_classes.append(accs[1])

        mean_acc = np.mean(accs, axis=1)
        print_mean_accuracy(mean_acc, t + 1, dataset.SETTING)
        # print('Task Trick', evaluate(model, dataset, last=True))

        model_stash['mean_accs'].append(mean_acc)

        if args.csv_log:
            csv_logger.log(mean_acc)
            csv_logger.log_class_detail(results)
            csv_logger.log_task_detail(results_mask_classes)
        if args.tensorboard:
            tb_logger.log_accuracy(np.array(accs), mean_acc, args, t)

        if hasattr(model.net, 'frozen'):
            model.net.frozen(t)

    end = time.time()
    time_train = round(end - start, 1)
    print('running time: ', time_train, ' s')
    if args.csv_log:
        csv_logger.log_time(time_train)
        csv_logger.add_bwt(results, results_mask_classes)
        csv_logger.add_forgetting(results, results_mask_classes)
        if model.NAME != 'icarl' and model.NAME != 'pnn' and model.NAME != 'our' and model.NAME != 'our_reservoir' and model.NAME != 'er_tricks' and model.NAME != 'onlinevt':
            csv_logger.add_fwt(results, random_results_class,
                               results_mask_classes, random_results_task)

    if args.tensorboard:
        tb_logger.close()
    if args.csv_log:
        csv_logger.write(vars(args))
