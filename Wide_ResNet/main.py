import os
import sys
import time
import argparse
import warnings
import numpy as np
from sklearn.metrics import accuracy_score
from wide_resnet import WideResNet
from utils import adjust_learning_rate, conv_init, get_hms, AverageMeter, Config

import torch
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
from torch.optim import SGD
from torch.autograd import Variable
from torch.utils.data import DataLoader

import torchvision
import torchvision.transforms as transforms
from torchvision.datasets import CIFAR10

def parse_option():

    parser = argparse.ArgumentParser("argument for training")

    # model hyperparamters
    parser.add_argument("--lr", default=0.1, type=float, help="learning rate")
    parser.add_argument("--depth", default=28, type=int, help="depth of the model")
    parser.add_argument("--widen_factor", default=10, type=int, help="widen factor of the model")
    parser.add_argument("--num_classes", default=10, type=int, help="number of classes")
    parser.add_argument("--dropout_rate", default=0.3, type=float, help="dropout rate")
    parser.add_argument("--epochs", type=int, default=200, help="number of training epochs")
    parser.add_argument("--batch_size", type=int, default=128, help="batch size")
    parser.add_argument("--lr_decay_epochs", type=str, default="60,120,160", help="where to decay lr, can be a list")
    parser.add_argument("--lr_decay_rate", type=float, default=0.2, help="decay rate for learning rate")
    parser.add_argument("--momentum", default=0.9, type=float, help="momentum for SGD optimizer")
    parser.add_argument("--weight_decay", default=5e-4, type=float, help="weight decay for SGD optimizer")
    parser.add_argument("--augment", type=str, default="meanstd", choices=["meanstd", "zac"], help="")
    
    # settings
    parser.add_argument("--resume", default="", type=str, metavar="PATH", help="path to latest checkpoint (default: none)")
    parser.add_argument("--start_epoch", default=1, type=int, help="start epoch")
    parser.add_argument("--test_only", action="store_true", default=False, help="test only")
    parser.add_argument("--save_freq", type=int, default=10, help="save frequency")
    parser.add_argument("--gpu", type=int, nargs="+", default=0, help="gpu ids to use")
    
    args = parser.parse_args()

    iterations = args.lr_decay_epochs.split(",")
    args.lr_decay_epochs = list([])
    for it in iterations:
        args.lr_decay_epochs.append(int(it))
    
    if isinstance(args.gpu, int):
        args.gpu = [args.gpu]
    return args

def train_one_epoch(args, train_loader, model, epoch, history):
    
    model.train()
    model.training = True
    args.lr = adjust_learning_rate(args.lr, epoch, args.lr_decay_rate, args.lr_decay_epochs)
    optimizer = SGD(
        model.parameters(), 
        lr=args.lr, 
        momentum=args.momentum, 
        weight_decay=args.weight_decay
    )
    criterion = nn.CrossEntropyLoss()
    acc_meter = AverageMeter()
    n_batch = (len(train_loader.dataset)//args.batch_size)+1

    print("\n=> Training Epoch #%d, LR=%.4f" %(epoch, args.lr))

    for batch_idx, (inputs, targets) in enumerate(train_loader):
        if torch.cuda.is_available():
            inputs, targets = inputs.cuda(), targets.cuda()

        optimizer.zero_grad()
        inputs, targets = Variable(inputs), Variable(targets)
        outputs = model(inputs)
        loss = criterion(outputs, targets)
        loss.backward()

        for param_group in optimizer.param_groups:
            param_group["lr"] = args.lr
        optimizer.step()

        _, predicts = torch.max(outputs.data, 1)
        acc = accuracy_score(targets.data.cpu().long().squeeze(), predicts.cpu().long().squeeze())
        acc_meter.update(acc, args.batch_size)

        sys.stdout.write("\r")
        sys.stdout.write("| Epoch [%d/%d] Iter[%d/%d]\t\tLoss: %.3f Acc: %.3f%%"
                %(epoch, args.epochs, batch_idx+1, n_batch, loss.item(), acc_meter.avg))
        sys.stdout.flush()
    
    history["acc"].append(acc_meter.avg)
    history["loss"].append(loss.item())   
    
def train(args, train_loader, valid_loader, model):

    model_folder = os.path.join("model/","WRN_{}_{}/".format(args.depth, args.widen_factor))
    os.makedirs(model_folder, exist_ok=True)
    history = {"acc": [], "loss": []}
    elapsed_time = 0

    print("Start training!")
    print("| Training Epochs = {}".format(args.epochs))
    print("| Initial Learning Rate = {}".format(args.lr))

    for epoch in range(args.start_epoch, args.epochs+1):

        start_time = time.time()
        train_one_epoch(args, train_loader, model, epoch, history)
        valid_one_epoch(args, valid_loader, model, epoch)
        elapsed_time += (time.time() - start_time)
        print("| Elapsed time : %d:%02d:%02d"  %(get_hms(elapsed_time)))
        
        if epoch % args.save_freq == 0:
            file_name = "ckpt_epoch_{}.pth".format(epoch)
            save_file = os.path.join(model_folder, file_name)
            print("==> Saving model at {}...".format(save_file))
            state = {
                "model": model.state_dict(),
                "epoch": epoch,
                "opt": args,
            }
            torch.save(state, save_file)
            del state

    print("=> Finish training")
    print("==> Saving model at {}...".format(save_file))
    file_name = "current.pth"
    save_file = os.path.join(model_folder, file_name)
    state = {
        "model": model.state_dict(),
        "epoch": epoch,
        "opt": args,
    }
    torch.save(state, save_file)
    del state
    np.save(model_folder + "history.npy".format(args.depth, args.widen_factor), history)
    torch.cuda.empty_cache()

def _test(args, test_loader, model):
    
    model.eval()
    model.training = False

    acc_meter = AverageMeter()
    criterion = nn.CrossEntropyLoss()

    for idx, (inputs, targets) in enumerate(test_loader):
        if torch.cuda.is_available():
            inputs, targets = inputs.cuda(), targets.cuda()

        with torch.no_grad():
            inputs, targets = Variable(inputs), Variable(targets)
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            _, predicts = torch.max(outputs.data, 1)

        acc = accuracy_score(targets.data.cpu().long().squeeze(), predicts.cpu().long().squeeze())
        acc_meter.update(acc, args.batch_size)

    return loss, acc_meter

def test(args, test_loader, model):
    loss, acc_meter = _test(args, test_loader, model)
    print("| Test Result\tAcc: %.3f%%" %(acc_meter.avg))

def valid_one_epoch(args, test_loader, model, epoch):
    loss, acc_meter = _test(args, test_loader, model)
    print("\n| Validation Epoch #%d\t\t\tLoss: %.4f Acc: %.2f%%" %(epoch, loss.item(), acc_meter.avg))

def main(args):
    np.random.seed(0)
    torch.manual_seed(0)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    # Constructing Model

    if args.resume != "":
        if os.path.isfile(args.resume):
            print("=> Loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume, map_location='cpu')
            test_only = args.test_only
            resume = args.resume
            args = checkpoint["opt"]
            args.test_only = test_only
            args.resume = resume
        else:
            checkpoint = None
            print("=> No checkpoint found at '{}'".format(args.resume))

    model = WideResNet(args.depth, args.widen_factor, args.dropout_rate, args.num_classes)

    if torch.cuda.is_available():
        model.cuda()
        model = torch.nn.DataParallel(model, device_ids=args.gpu)

    if args.resume != "":
        model.load_state_dict(checkpoint["model"])
        args.start_epoch = checkpoint["epoch"] + 1
        print(
            "=> Loaded successfully '{}' (epoch {})".format(
                args.resume, checkpoint["epoch"]
            )
        )
        del checkpoint
        torch.cuda.empty_cache()
    else:
        model.apply(conv_init)

    # Loading Dataset    

    if args.augment == "meanstd":
        transform_train = transforms.Compose([
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(Config.CIFAR10_mean, Config.CIFAR10_std),
        ])

        transform_test = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(Config.CIFAR10_mean, Config.CIFAR10_std),
        ])
    elif args.augment == "zac": 
        # To Do: ZCA whitening
        pass
    else:
        raise NotImplementedError

    print("| Preparing CIFAR-10 dataset...")
    sys.stdout.write("| ")
    trainset = CIFAR10(root="./data", train=True, download=True, transform=transform_train)
    testset = CIFAR10(root="./data", train=False, download=False, transform=transform_test)
    
    train_loader = DataLoader(trainset, batch_size=args.batch_size, shuffle=True, num_workers=2)
    test_loader = DataLoader(testset, batch_size=args.batch_size, shuffle=False, num_workers=2)

    # Test only

    if args.test_only:
        if args.resume != "":
            test(args, test_loader, model)
            sys.exit(0)
        else:
            print("=> Test only model need to resume from a checkpoint")
            raise RuntimeError

    train(args, train_loader, test_loader, model)
    test(args, test_loader, model)


if __name__ == "__main__":

    warnings.simplefilter("once", UserWarning)
    args = parse_option()
    main(args)
