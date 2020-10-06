import os
import time
import pickle
import argparse
import warnings
import numpy as np
import torch
import torch.utils.data as data
import torch.nn as nn
import torch.nn.init as init
import torch.nn.functional as F
import torchvision
import torchvision.transforms as transforms

from torch.autograd import Variable
from torch.utils.data import DataLoader
from PIL import Image
from sklearn.metrics import accuracy_score
from typing import Any, Callable, List, Optional, Tuple
from wide_resnet import WideResNet

from utils import (
    AverageMeter, adjust_learning_rate, get_hms,
)

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
    parser.add_argument("--augment", type=str, default="meanstd", choices=["meanstd", "zac"], help="")
    
    # settings
    parser.add_argument("--resume", default="", type=str, metavar="PATH", help="path to latest checkpoint (default: none)")
    parser.add_argument("--test_only", action="store_true", default=False, help="test only")
    parser.add_argument("--save_freq", type=int, default=10, help="save frequency")

    args = parser.parse_args()

    return args

def train_one_epoch(args, train_loader, model, criterion, optimizer) -> Any:
    n_batch = (len(train_loader.dataset)//batch_size)+1
    model.train()

    acc_meter = AverageMeter()

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

        predicts = out.argmax(dim=1)
        acc = accuracy_score(targets.data.cpu().long().squeeze(), predicts.cpu().long().squeeze())
        acc_meter.update(acc, args.batch_size)

        sys.stdout.write("\r")
        sys.stdout.write("| Epoch [%3d/%3d] Iter[%3d/%3d]\t\tLoss: %.4f Acc: %.3f%%"
                %(epoch, args.epochs, batch_idx+1, n_batch, loss.item(), acc_meter.avg))
        sys.stdout.flush()

        torch.cuda.synchronize()
        
    return loss_meter, acc_meter
    
def train(args, train_loader, model):

    model_folder = os.path.join("model/","WRN_{}_{}/".format(depth, widen_factor))

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(params, lr=lr, momentum=0.9, weight_decay=0.0005)
    criterion = nn.CrossEntropyLoss()

    print("Start training!")

    elapsed_time = 0
    history = {"acc": [], "loss": []}

    print("| Training Epochs = {}".format(args.epochs))
    print("| Initial Learning Rate = {}".format(args.lr))

    for epoch in range(1, args.epochs+1):

        start_time = time.time()
        args.lr = adjust_learning_rate(args.lr, epoch, decay_epochs)

        print("\n=> Training Epoch #%d, LR=%.4f" %(epoch, args.lr))
        loss, acc = train_one_epoch(args, train_loader, model, criterion, optimizer)
        epoch_time = time.time() - start_time
        elapsed_time += epoch_time
        print("| Elapsed time : %d:%02d:%02d"  %(get_hms(elapsed_time)))

        history["acc"].append(acc.avg)
        history["loss"].append(loss.avg)

        if epoch % save_freq == 0:
            save_file = os.path.join(model_folder, file_name)
            print("==> Saving model at {}...".format(save_file))
            state = {
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "epoch": epoch,
                "opt": opt,
            }
            
            torch.save(state, save_file)
    del state
    
    save_model(model, optimizer, args.epochs, "current.pth", model_folder)
    torch.cuda.empty_cache()
    
    print("Finish Training!")
    np.save("./model/WRN_{}_{}/history.npy".format(args.depth, args,widen_factor), history)

def test(args, test_loader, model):

    if torch.cuda.is_available():
        model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))
        cudnn.benchmark = True
    
    model.eval()
    model.training = False

    acc_meter = AverageMeter()
    for idx, (inputs, targets) in enumerate(test_loader):
        if torch.cuda.is_available():
            inputs, targets = inputs.cuda(), targets.cuda()

        inputs, targets = Variable(inputs), Variable(targets)
        outputs = model(inputs)
        _, predicts = torch.argmax(outputs.data, dim=1)

        acc = accuracy_score(targets.data.cpu().long().squeeze(), predicts.cpu().long().squeeze())
        acc_meter.update(acc, args.batch_size)

    print("| Test Result\tAcc: %.3f%%" %(acc_meter.avg))
    return acc_meter

def main(args):
    np.random.seed(0)
    torch.manual_seed(0)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    # Constructing Model

    if args.resume:
        if os.path.isfile(args.resume):
            print("=> Loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume, map_location="cpu")
            test_only = args.test_only
            args = checkpoint["opt"]
            args.test_only = test_only
            args.resume = True
        else:
            checkpoint = None
            print("=> No checkpoint found at '{}'".format(args.resume))

    model = WideResNet(args.depth, args.widen_factor, args.dropout_rate, args.num_classes)

    if args.resume:
        model.load_state_dict(checkpoint["model"])
        print(
            "=> Loaded successfully '{}' (epoch {})".format(
                args.resume, checkpoint["epoch"]
            )
        )
        del checkpoint
        torch.cuda.empty_cache()

    if torch.cuda.is_available():
        model.cuda()
        model = torch.nn.DataParallel(model, device_ids=range(torch.cuda.device_count()))

    # Loading Dataset    

    if args.augment == "meanstd":
        transform_train = transforms.Compose([
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(cf.mean[args.dataset], cf.std[args.dataset]),
        ])

        transform_test = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(cf.mean[args.dataset], cf.std[args.dataset]),
        ])
    elif args.augment == "zac": 
        # To Do: ZCA whitening
        pass
    else:
        raise NotImplementedError

    print("| Preparing CIFAR-10 dataset...")
    sys.stdout.write("| ")
    trainset = torchvision.datasets.CIFAR10(root="./data", train=True, download=True, transform=transform_train)
    testset = torchvision.datasets.CIFAR10(root="./data", train=False, download=False, transform=transform_test)
    
    train_loader = torch.utils.data.DataLoader(trainset, batch_size=args.batch_size, shuffle=True, num_workers=2)
    test_loader = torch.utils.data.DataLoader(testset, batch_size=100, shuffle=False, num_workers=2)

    # Test only

    if args.test_only:
        if args.resume:
            
            test(args, test_loader, model)
            sys.exit(0)
        else:
            print("=> Test only model need to resume from a checkpoint")
            raise RuntimeError

    train(args, train_loader, model)
    test(args, test_loader, model)


if __name__ == "__main__":

    warnings.simplefilter("once", UserWarning)
    args = parse_option()
    main(args)
                                                                                                                                   125,31        Bo
