import torch
from torch import nn
from torch.optim import Adam
from torch.nn import functional as F
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter
import datetime
import os
from torch.optim.lr_scheduler import ReduceLROnPlateau
from .statistic import calculate, old_calculate


class Trainer:
    def __init__(self, args, model, train_data, valid_data, valid_infer_data, test_infer_data, t_vocab):
        self.args = args
        cuda_condition = torch.cuda.is_available() and self.args.with_cuda
        self.device = torch.device("cuda:0" if cuda_condition else "cpu")
        if cuda_condition and torch.cuda.device_count() > 1:
            self.wrap = True
            model = nn.DataParallel(model)
        else:
            self.wrap = False
        self.model = model.to(self.device)
        self.train_data = train_data
        self.valid_data = valid_data
        self.valid_infer_data = valid_infer_data
        self.test_infer_data = test_infer_data
        self.optim = Adam(self.model.parameters(), lr=self.args.lr, weight_decay=self.args.weight_decay)
        if self.args.lr_scheduler:
            self.scheduler = ReduceLROnPlateau(self.optim, 'max', verbose=True, patience=0, factor=0.1, min_lr=1e-5)
        self.clip = self.args.clip
        self.writer_path = '{}_{}_{}'.format('relation' if args.relation_path else 'Naive', args.dataset,
                                             datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S'))
        print(self.writer_path)
        self.tensorboard_writer = SummaryWriter(os.path.join('run', self.writer_path))
        self.writer = open(os.path.join('run', self.writer_path, 'experiment.txt'), 'w')
        print(self.args, file=self.writer, flush=True)
        self.iter = -1
        self.t_vocab = t_vocab
        self.best_epoch, self.best_f1 = 0, float('-inf')
        self.accu_steps = self.args.accu_batch_size // self.args.batch_size
        self.criterion = nn.NLLLoss(ignore_index=0)
        self.unk_shift = self.args.unk_shift
        if self.args.relation_path or self.args.absolute_path:
            print(
                "Total Parameters: {}*1e6".format(sum([p.nelement() for _, p in self.model.named_parameters()]) // 1e6),
                file=self.writer, flush=True)
        else:
            model_parameters = []
            for name, param in self.model.named_parameters():
                if 'path' in name:
                    continue
                else:
                    model_parameters.append(param)
            print("Total Parameters: {}*1e6".format(sum([p.nelement() for p in model_parameters]) // 1e6),
                  file=self.writer, flush=True)

    def load(self, path):
        dic = torch.load(path, map_location='cpu')
        load_pre = ''
        model_pre = ''
        print(dic.keys())
        for key, _ in dic.items():
            if 'module.' in key:
                load_pre = 'module.'
            else:
                load_pre = ''
            break
        for key, _ in self.model.state_dict().items():
            if 'module.' in key:
                model_pre = 'module.'
            else:
                model_pre = ''
            break
        if load_pre == '' and model_pre == 'module.':
            temp_dict = dict()
            for key, value in dic.items():
                temp_dict[model_pre + key] = value
            dic = temp_dict
        elif model_pre == '' and load_pre == 'module.':
            temp_dict = dict()
            for key, value in dic.items():
                temp_dict[key.replace(load_pre, model_pre)] = value
            dic = temp_dict
        temp_dict = dict()
        ori_dic = self.model.state_dict()
        for key, value in dic.items():
            if key in ori_dic and ori_dic[key].shape == value.shape:
                temp_dict[key] = value
        dic = temp_dict
        print(dic.keys())
        for key, value in self.model.state_dict().items():
            if key not in dic:
                dic[key] = value
        self.model.load_state_dict(dic)
        print('Load Pretrain model => {}'.format(path))

    def train(self, epoch):
        self.iteration(epoch, self.train_data)

    def test(self, epoch):
        self.iteration(epoch, self.valid_data, train=False)

    def label_smoothing_loss(self, logits, targets, eps=0, reduction='mean'):
        if eps == 0:
            return self.criterion(logits, targets)
        K = logits.shape[-1]
        one_hot_target = F.one_hot(targets, num_classes=K)
        l_targets = (one_hot_target * (1 - eps) + eps / K).detach()
        loss = -(logits * l_targets).sum(-1).masked_fill(targets == 0, 0.0)
        if reduction == 'mean':
            return loss.sum() / torch.count_nonzero(targets)
        elif reduction == 'sum':
            return loss.sum()
        return loss

    def iteration(self, epoch, data_loader, train=True):
        str_code = "train" if train else "valid"
        data_iter = tqdm(enumerate(data_loader),
                         desc="EP_%s:%d" % (str_code, epoch),
                         total=len(data_loader),
                         bar_format="{l_bar}{r_bar}")
        avg_loss = 0.0
        if train:
            self.optim.zero_grad()
        for i, data in data_iter:
            data = {key: value.to(self.device) if torch.is_tensor(value) else value for key, value in data.items()}
            if train:
                self.model.train()
                # import pdb;pdb.set_trace()
                out = self.model(data)
                labels = data['target']
                loss_fct = torch.nn.CrossEntropyLoss()
                loss = loss_fct(out, labels)
                accu_loss = loss / self.accu_steps
                accu_loss.backward()
                if self.clip > 0:
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.clip)
                if (i + 1) % self.accu_steps == 0:
                    self.optim.step()
                    self.optim.zero_grad()
            else:
                self.model.eval()
                with torch.no_grad():
                    out = self.model(data)
                    labels = data['target']
                    loss_fct = torch.nn.CrossEntropyLoss()
                    loss = loss_fct(out, labels)
                    # loss = self.criterion(out.view(out.shape[0] * out.shape[1], -1),
                    #                       data['f_target'].view(-1))  # avg at every step
            avg_loss += loss.item()
            post_fix = {
                'str': str_code,
                "epoch": epoch,
                "iter": i,
                "Iter loss": loss.item(),
            }
            if train:
                self.iter += 1
                if self.tensorboard_writer is not None:
                    self.tensorboard_writer.add_scalar('Loss', post_fix['Iter loss'], self.iter)
        avg_loss = avg_loss / len(data_iter)
        print("EP%d_%s, avg_loss=" % (epoch, str_code), avg_loss, file=self.writer, flush=True)
        print('-------------------------------------', file=self.writer, flush=True)

        if self.args.save and train:
            save_dir = './checkpoint'
            if not os.path.exists(save_dir):
                os.makedirs(save_dir)
            torch.save(self.model.state_dict(),
                       os.path.join(save_dir, "{}_{}.pth".format(self.writer_path, epoch)))

    def predict(self, epoch, test=True):
        if test:
            data_loader = self.test_infer_data
            str_code = 'test'
        else:
            data_loader = self.valid_infer_data
            str_code = 'valid'

        def get_ref_strings(nums):
            infer_file = data_loader.dataset.data[:nums]
            refs = []
            for sample in infer_file:
                target, _, _, _, _, _, _, _ = sample.strip().split('\t')
                refs.append(target.split('|'))
            return refs

        def filter_special_convert(lis_, e_voc_=None):
            if self.t_vocab.eos_index in lis_:
                lis = lis_[:lis_.index(self.t_vocab.eos_index)]
            else:
                lis = lis_
            id_lis = list(filter(lambda x: x not in self.t_vocab.special_index, lis))[:self.args.max_target_len - 1]
            if e_voc_ is not None:
                str_lis_ = []
                for token in id_lis:
                    if self.t_vocab.has_idx(token):
                        str_lis_.append(self.t_vocab.re_find(token))
                    else:
                        str_lis_.append(e_voc_[token])
            else:
                str_lis_ = [self.t_vocab.re_find(token) for token in id_lis]
            return id_lis, str_lis_

        def write_strings(predict, original, ref_file_, pred_file_):
            for p, o in zip(predict, original):
                ref_file_.write(' '.join(str(o)) + '\n')
                pred_file_.write(' '.join(str(p)) + '\n')

        data_iter = tqdm(enumerate(data_loader),
                         desc="EP_%s:%d" % (str_code + '_infer', epoch),
                         total=len(data_loader),
                         bar_format="{l_bar}{r_bar}")
        ref_file_name = os.path.join('run', self.writer_path, 'ref_{}.txt'.format(str_code))
        predicted_file_name = os.path.join('run', self.writer_path,
                                           'pred_{}_{}.txt'.format(str_code, epoch))
        with open(ref_file_name, 'w') as ref_file, open(predicted_file_name, 'w') as pred_file:
            predict_strings = []
            ref_strings = []
            predict_idx = []
            ref_idx = []
            for i, data in data_iter:
                self.model.eval()
                data = {key: value.to(self.device) if torch.is_tensor(value) else value for key, value in
                        data.items()}
                with torch.no_grad():
                    if self.args.pointer:
                        content_e, voc_len = data['content_e'], data['voc_len']
                    else:
                        content_e, voc_len = None, None
                    out = self.model(data)
                    predict_idx = out.argmax(dim=-1).tolist()
                    label_idx = data['target'].tolist()
                    for sample in predict_idx:
                        predict_strings.append(sample)
                    for sample in label_idx:
                        ref_strings.append(sample)
            # ref_strings = get_ref_strings(nums=len(predict_strings))
            write_strings(predict_strings, ref_strings, ref_file, pred_file)
        acc = sum([1 for p, r in zip(predict_strings, ref_strings) if p == r]) / len(predict_strings)
        precision, recall, f1 = acc, acc, acc
        print(
            "{} precision={:.6f}, recall={:.6f}, f1={:.6f}".format(str_code, precision, recall, f1), file=self.writer,
            flush=True)
        if not test and self.args.lr_scheduler:
            self.scheduler.step(f1)
        if not test:
            if f1 >= self.best_f1:
                self.best_f1 = f1
                self.best_epoch = epoch
            print("Best Valid At EP{}, best_f1={}".format(self.best_epoch, self.best_f1), file=self.writer,
                  flush=True)
        print('-------------------------------------', file=self.writer, flush=True)
