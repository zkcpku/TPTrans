from torch.utils.data import Dataset
import os
import torch
from .process_utils import convert_line, decoder_process, row_process, content_process, path_process, r_path_process, \
    make_extended_vocabulary


class PathAttenDataset(Dataset):
    '''
    Please note for the dict keys presented in this file:

    The 'path' is the relative path, the 'paths_mask' is mask for padded relative path
    The 'r_path' means the path to 'r'oot, so it is the absolute path, and the 'r_paths_mask' is mask for padded absolute path

    The 'f_source' is the function name as decoder input, and the 'f_target' is the decoder's gold target

    The 'content' is the code tokens, the 'content_mask' is mask for token padding

    The 'path_map' is the matrix M for mapping, see appendix about the efficient computation of relative path encoding for details
    The 'r_path_idx' is used for reduce cost of absolute path, also see appendix about absolute path encoding

    The 'named' and 'row' are some additional structure information, but they are not much useful, so you can also ignore them

    The 'e_voc', 'e_voc_', 'voc_len' and 'content_e' are used for pointer network
    '''

    def __init__(self, args, s_vocab, t_vocab, type_):
        self.on_memory = args.on_memory
        self.dataset_dir = os.path.join('./data', args.dataset)
        self.s_vocab = s_vocab
        self.t_vocab = t_vocab
        self.args = args
        self.type_ = type_
        assert type_ in ['train', 'test', 'valid']
        if self.on_memory:
            self.json_path = os.path.join(self.dataset_dir, type_ + '.txt')
            with open(self.json_path, 'r') as f:
                self.data = f.readlines()
            self.corpus_line = len(self.data)
        else:
            self.json_path = os.path.join(self.dataset_dir, type_ + '.txt')
            self.corpus_line = 0
            with open(self.json_path, 'r') as f:
                for _ in f:
                    self.corpus_line += 1
            self.file = open(self.json_path, 'r')
        if self.args.tiny_data > 0:
            self.corpus_line = self.args.tiny_data
        self.hop = self.args.hop
        # self.rp_sample = self.args.rp_sample

    def __len__(self):
        return self.corpus_line

    def __getitem__(self, item):
        assert item < self.corpus_line
        data = self.get_corpus_line(item)
        sample = self.process(data)
        return {key: value if torch.is_tensor(value) or isinstance(value, dict) else torch.tensor(value) for key, value
                in sample.items()}

    def process(self, data):
        if self.args.pointer:
            assert self.args.uni_vocab, 'separate vocab not support'
            e_voc, e_voc_, voc_len = make_extended_vocabulary(data['content'], self.s_vocab)
        else:
            e_voc, e_voc_, voc_len = None, None, None
        f_source, f_target = decoder_process(data['target'], self.t_vocab, self.args.max_target_len,
                                             e_voc, self.args.pointer)
        row_ = row_process(data['row'], self.args.max_code_length)
        content_, content_mask_, named_, content_e = content_process(data['content'], data['named'], self.s_vocab,
                                                                     self.args.max_code_length, e_voc,
                                                                     self.args.pointer)
        paths_map_, paths_, paths_mask_ = path_process(data['paths'], data['paths_map'], self.args.max_path_num,
                                                       self.args.max_code_length, self.args.path_embedding_num,
                                                       self.args.max_path_length, convert_hop=self.hop)
        r_paths_, r_path_idx_, r_paths_mask_ = r_path_process(data['r_paths'], data['r_path_idx'],
                                                              self.args.max_r_path_num,
                                                              self.args.max_code_length, self.args.max_r_path_length,
                                                              self.args.path_embedding_num, convert_hop=self.hop)
        cls_num = data['target']
        cls_num = ''.join(cls_num)
        cls_num = int(cls_num)
        data_dic = {'f_source': f_source, 'f_target': f_target, 'content': content_, 'content_mask': content_mask_,
                    'path_map': paths_map_, 'paths': paths_, 'paths_mask': paths_mask_, 'named': named_, 'row': row_,
                    'r_paths': r_paths_, 'r_path_idx': r_path_idx_, 'r_paths_mask': r_paths_mask_,
                    'target': cls_num}
        if self.args.pointer:
            data_dic['e_voc'] = e_voc
            data_dic['e_voc_'] = e_voc_
            data_dic['voc_len'] = voc_len
            data_dic['content_e'] = content_e
        return data_dic

    def get_corpus_line(self, item):
        if self.on_memory:
            data = self.data[item]
            return convert_line(data)
        else:
            if item == 0:
                self.file.close()
                self.file = open(self.json_path, 'r')
            line = self.file.__next__()
            if line is None:
                self.file.close()
                self.file = open(self.json_path, 'r')
                line = self.file.__next__()
            data = convert_line(line)
            return data


def collect_fn(batch):
    data = dict()
    max_content_len, max_target_len = 0, 0
    for sample in batch:
        c_l = torch.count_nonzero(sample['content_mask']).item()
        f_l = torch.count_nonzero(sample['f_source']).item()
        if c_l > max_content_len: max_content_len = c_l
        if f_l > max_target_len: max_target_len = f_l
    data['f_source'] = torch.stack([b['f_source'] for b in batch], dim=0)[:, :max_target_len]
    data['f_target'] = torch.stack([b['f_target'] for b in batch], dim=0)[:, :max_target_len]
    data['content'] = torch.stack([b['content'] for b in batch], dim=0)[:, :max_content_len]
    data['content_mask'] = torch.stack([b['content_mask'] for b in batch], dim=0)[:, :max_content_len]
    data['path_map'] = torch.stack([b['path_map'] for b in batch], dim=0)[:, :max_content_len, :max_content_len]
    data['paths'] = torch.stack([b['paths'] for b in batch], dim=0)
    data['paths_mask'] = torch.stack([b['paths_mask'] for b in batch], dim=0)
    data['named'] = torch.stack([b['named'] for b in batch], dim=0)[:, :max_content_len]
    data['row'] = torch.stack([b['row'] for b in batch], dim=0)[:, :max_content_len]
    data['r_paths'] = torch.stack([b['r_paths'] for b in batch], dim=0)
    data['r_path_idx'] = torch.stack([b['r_path_idx'] for b in batch], dim=0)[:, :max_content_len]
    data['r_paths_mask'] = torch.stack([b['r_paths_mask'] for b in batch], dim=0)
    if 'e_voc' in batch[0]:
        data['e_voc'] = [b['e_voc'] for b in batch]
        data['e_voc_'] = [b['e_voc_'] for b in batch]
        max_voc_len = torch.max(torch.stack([b['voc_len'] for b in batch], dim=0)).item()
        data['voc_len'] = torch.tensor(
            [max_voc_len for _ in batch])  # we set e voc len equal for all data in batch, for data parallel
        data['content_e'] = torch.stack([b['content_e'] for b in batch], dim=0)[:, :max_content_len]
    data['target'] = torch.stack([b['target'] for b in batch], dim=0)
    return data
