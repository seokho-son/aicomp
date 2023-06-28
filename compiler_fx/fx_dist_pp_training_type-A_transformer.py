#
# Copyright (c) 2023-present, ETRI, All rights reserved.
#
#  This is a PoC that performs a GPipe-style pipeline-parallel training based on the FX IR.
#
#   In this PoC, FX compile generates FX IR,
#       and each process is responsible for a subset of the entire FX IR,
#       and pipeline parallel training is executed across N processes.
#
#   Micro-batch is supported in this PoC, and applied to the Transformer model (CPU version)
#
#       The Transformer source is adapted from the pytorch tutorial (https://github.com/pytorch/tutorials/blob/main/advanced_source/ddp_pipeline.py)
#   [prerequisite]
#       $ pip3 install torchtext
#       $ pip3 install torchdata
#
#
#  Sample Usage:
#      <machine #0>
#            torchrun --nproc_per_node=1 --nnodes=2 --node_rank=0
#                  --master_addr="X.X.X.X" --master_port=29500 fx_dist_pp_training_type-A_transformer.py
#      <machine #1>
#            torchrun --nproc_per_node=1 --nnodes=2 --node_rank=1
#                  --master_addr="X.X.X.X" --master_port=29500 fx_dist_pp_training_type-A_transformer.py
#

import sys
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import tempfile
from torch.nn import TransformerEncoder, TransformerEncoderLayer

from torch import Tensor, Size
from torch.nn.parameter import Parameter, UninitializedParameter
from torch.nn import init
from torch.optim import Adam
from torch import fx
from torch.fx.node import Node
import copy
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import time

import torch.distributed as dist
import datetime
#import torch.distributed.rpc as rpc

import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))

from torch.fx.graph_module import GraphModule
from torch.fx.passes.split_module import split_module


torch.manual_seed(42)

#
# Total host count
#
#num_rank=N
num_rank=2
#num_rank=4  
#num_rank=6  
#num_rank=8  
#num_rank=16


class Encoder(nn.Module):
    def __init__(self, ntoken, ninp, dropout=0.5):
        super(Encoder, self).__init__()
        self.pos_encoder = PositionalEncoding(ninp, dropout)
        self.encoder = nn.Embedding(ntoken, ninp)
        self.ninp = ninp
        self.init_weights()

    def init_weights(self):
        initrange = 0.1
        self.encoder.weight.data.uniform_(-initrange, initrange)

    def forward(self, src):
        # Need (S, N) format for encoder.
        src = src.t()
        src = self.encoder(src) * math.sqrt(self.ninp)
        return self.pos_encoder(src)

class Decoder(nn.Module):
    def __init__(self, ntoken, ninp):
        super(Decoder, self).__init__()
        self.decoder = nn.Linear(ninp, ntoken)
        self.init_weights()

    def init_weights(self):
        initrange = 0.1
        self.decoder.bias.data.zero_()
        self.decoder.weight.data.uniform_(-initrange, initrange)

    def forward(self, inp):
        # Need batch dimension first for output of pipeline.
        return self.decoder(inp).permute(1, 0, 2)


#
# slice wrapping function for FX's symbolic_tracing
#
def pe_slice(x, y):
    sizes = x.size(0)
    return y[:sizes, :]

torch.fx.wrap('pe_slice')

class PositionalEncoding(nn.Module):

    def __init__(self, d_model, dropout=0.1, max_len=5000):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x):
        #x = x + self.pe[:x.size(0), :]    # original

        #
        # wrap slice for FX's symbolic_tracing
        #
        x = x + pe_slice(x, self.pe)
        return self.dropout(x)



import torch
from torchtext.datasets import WikiText2
from torchtext.data.utils import get_tokenizer
from torchtext.vocab import build_vocab_from_iterator



gm = None

# LossWrapper: cited from PiPPy
class LossWrapper(torch.nn.Module):
    def __init__(self, module, loss_fn):
        super().__init__()
        self.module = module
        self.loss_fn = loss_fn

    def forward(self, *args, **kwargs):
        raise NotImplementedError("LossWrapper: no forward implementation")

# SimpleLossWrapper: cited from PiPPy
class SimpleLossWrapper(LossWrapper):
    def forward(self, x, targets):
        out1 = self.module(x)
        return self.loss_fn(out1, targets)


def get_total_params(module: torch.nn.Module):
    total_params = 0
    for param in module.parameters():
        total_params += param.numel()
    return total_params



class Simple_split_test(object):
    def __init__(self):
        self.initialize_comm()
        self.model_ir = []
        self.range_metadata = []

    def initialize_comm(self):

        if dist.is_initialized():
            print(f"Communication already initialized")
            return


        self.rank = int(os.environ["RANK"])
        self.world_size = int(os.environ["WORLD_SIZE"])
        self.master_addr = os.getenv("MASTER_ADDR")
        self.master_port = os.getenv("MASTER_PORT")
        self.stage = 0


        #
        print(f" --- rank:{self.rank}, world_size:{self.world_size}, master:{self.master_addr}, port:{self.master_port}")

        self.backend = "gloo"
        init_method = "tcp://" + str(self.master_addr) + ":" + str(self.master_port)

        dist.init_process_group(backend=self.backend, rank=self.rank, world_size=self.world_size, init_method=init_method)

        #
        print(f" --- rank:{dist.get_rank()}, world_size:{dist.get_world_size()}")

        #options = rpc.TensorPipeRpcBackendOptions(num_worker_threads=10, rpc_timeout=30)

        #rpc.init_rpc(f"worker{self.rank}", rank=self.rank, world_size=self.world_size, rpc_backend_options=options,)

        #print(f" --- after init_rpc -- rank:{dist.get_rank()}, world_size:{dist.get_world_size()}")

        # rpc.shutdown()


    def simple_split(self, g: fx.Graph):

        length = g.nodes.__len__()

        mod_cnt = 0
        for n in g.nodes:
            if n.op == 'call_module':
                mod_cnt = mod_cnt + 1


        # simple assert
        assert mod_cnt >= num_rank, f"Model length:{length} is smaller than # of processes:{num_rank}"

        target_cnt = mod_cnt // num_rank
        print(f"simple_split >> length:{length}, num_rank:{num_rank}, mod_cnt:{mod_cnt}, target_cnt:{target_cnt}")

        k, m_cnt, cnt = 0, 0, 0
        for n in g.nodes:
            if n.op == 'call_module':
                m_cnt = m_cnt + 1

            if m_cnt == target_cnt and k < num_rank-1:
                self.range_metadata.append((k, n.name))
                k = k + 1
                m_cnt = 0

            cnt = cnt + 1

            if cnt == length:
                break

        # DEBUG
        print(f" >>> cnt: {cnt}, k:{k}, n:{n.name}, mod_cnt:{mod_cnt}, target_cnt:{target_cnt}")
        if len(self.range_metadata) <  num_rank:
            self.range_metadata.append((k, n.name))

        # DEBUG
        print(f" ------------------------------------------------------------")
        print(self.range_metadata)
        print(f" ------------------------------------------------------------")



    def metadata_transfer(self):

        criterion = nn.CrossEntropyLoss()
        wrapper = SimpleLossWrapper(model, criterion)

        global gm
        gm = fx.symbolic_trace(wrapper)

        for n in gm.graph.nodes:
            print(f"n.op:{n.op}, n.name:{n.name}, n.target:{n.target}, n.args:{n.args}, n.all_input_nodes:{n.all_input_nodes}")
        print(f"------------------------------------------------------------")
        self.train_data_size = train_data.size(0)

        self.device = torch.device("cpu")

        self.model_ir.append(gm)
        self.stage = self.rank # TODO


        if self.rank == 0:
            self.simple_split(gm.graph)
            dist.broadcast_object_list(self.range_metadata, src=0, device=self.device)

            print(f" >> worker:{self.rank} ==> range metadata {self.range_metadata} transfer to all other workers")

        else:
            for i in range(num_rank):
                self.range_metadata.append(None)

            dist.broadcast_object_list(self.range_metadata, src=0, device=self.device)

            print(f" worker: {self.rank} <==  range metadata:{self.range_metadata} transferred")
            print(f" ---------------------------------")




# stage_backward function: cited from PiPPy
def stage_backward(
    stage_output,
    output_grads,
    input_values,
    outputs_with_grads_idxs: List[int],
):
    #print(f"** stage_backward ** stage_output:{stage_output}, output_grads:{output_grads}, input_values:{input_values}, outputs_with_grads_idxs: {outputs_with_grads_idxs}")

    stage_output_with_grads = [
        stage_output[i] for i in outputs_with_grads_idxs
    ]
    output_grads_with_grads = [
        output_grads[i] for i in outputs_with_grads_idxs
    ]

    stage_output_tensors = []
    output_grad_tensors = []

    def extract_tensors_with_grads(output_val, grad_val):
        if isinstance(output_val, torch.Tensor):
            if not output_val.requires_grad and output_val.grad_fn is None:
                return
            stage_output_tensors.append(output_val)
            output_grad_tensors.append(grad_val)
        elif isinstance(output_val, (tuple, list)):
            if grad_val is None:
                return
            for ov, gv in zip(output_val, grad_val):
                extract_tensors_with_grads(ov, gv)
        elif isinstance(output_val, dict):
            if grad_val is None:
                return
            for k in output_val.keys():
                extract_tensors_with_grads(output_val[k], grad_val[k])
        else:
            print(f"... ignored in this case")
            pass

    extract_tensors_with_grads(stage_output_with_grads, output_grads_with_grads)

    torch.autograd.backward(stage_output_tensors, grad_tensors=output_grad_tensors)

    grad_inputs = []
    for val in input_values:
        if isinstance(val, torch.Tensor):
        #if isinstance(val, torch.Tensor) and val.is_floating_point():
            grad_inputs.append(val.grad)
        else:
            grad_inputs.append(None)

    barrier_token = None
    return grad_inputs, barrier_token



class FXRun2:

    #def __init__(self, split_info: Simple_split_test, device, mbsize):
    def __init__(self, split_info: Simple_split_test, device, train_data_size, mbsize): # TODO

        self.mod = split_info.model_ir[0]
        self.graph = self.mod.graph
        self.modules = dict(self.mod.named_modules())
        self.mbsize = mbsize  
        self.env2: List[Dict[str, Node]] = [{} for _ in range(mbsize)]
        self.range_metadata = split_info.range_metadata
        self.rank = split_info.rank
        self.world_size = split_info.world_size
        self.device = device
        self.stage = split_info.stage
        self.loss: List[Any] = [None for _ in range(mbsize)]
        self.fwd_cache2: List[Dict[str, Tuple[Any, List[torch.Tensor]]]] = [{} for _ in range(mbsize)]
        self.grads2: List[Dict[str, Any]] = [{} for _ in range(mbsize)]
        self.train_data_size = split_info.train_data_size # TODO
        #self.window_size = 1 # TODO

        # TODO
        self.ds_type2id = {
            Tensor: 100,
            tuple: 101,
            list: 102,
            Size: 103,
            int: 104, }

        self.ds_id2type = {v:k for k, v in self.ds_type2id.items()}
        
        self.tensor_type2id = {
            torch.float32: 0,
            torch.float64: 1,
            torch.complex64: 2,
            torch.complex128: 3,
            torch.float16: 4,
            torch.bfloat16: 5,
            torch.uint8: 6,
            torch.int8: 7,
            torch.int16: 8,
            torch.int32: 9,
            torch.int64: 10,
            torch.bool: 11, }

        self.tensor_id2type = {v:k for k,v in self.tensor_type2id.items()}


    def get_range(self, rank, g:fx.Graph) -> (Node, Node):

        #print(f"rank:{self.rank} range_metadata: {self.range_metadata}")

        if rank == 0:
            from_node_name = "-1"
            for n in g.nodes:
                if n.op == 'placeholder':
                    from_node_name = n.name
                    #print(f">>>> get_range: n.op == 'placeholder' --> from_node_name:{from_node_name}")
                break
        else:
            from_node_name = self.range_metadata[rank-1][1]

        to_node_name = self.range_metadata[rank][1]

        for n in g.nodes:
            if from_node_name == "-1":
                from_node = n
                break

            if n.name == from_node_name:
                from_node = n
                break

        for n in reversed(g.nodes):
            if n.name == to_node_name :
                to_node = n
                break

        if rank == 0:
            return (from_node, to_node)
        else:
            return (from_node._next, to_node)



    def print_range(self):
        from_, to_ = self.get_range(self.rank, self.graph)

        print(f" # rank = {self.rank}, from_:{from_.name}, to_:{to_.name}")

        cur = from_ # first node assigned to the process#{rank} in metadata_range
        while cur != to_:
            print(f" ---- node:{cur.name}")
            cur = cur._next
        print(f" ---- node:{cur.name}")  # last node assigned to the process#{rank} in metadata_range
        print(f" -------------------------------")


    # TODO
    #def get_destination4(self, node):
    #    for n in self.mod.graph.nodes:
    #        if n.name == node.name:
    #            return n._prev


    def get_destination(self, input_nodes, lst_):
    #def get_destination(self, input_nodes, set_):
        k = None
        for i, m in enumerate(input_nodes):
            for n in self.graph.nodes:
                if n.name == m.name:
                    #if m.op == 'call_module' or m.op == 'call_method':
                    #if m.op == 'call_module' or m.op == 'call_method' or m.op == 'call_function':
                    #if m.op == 'call_module' or m.op == 'call_method' or m.op == 'call_function' or m.op == 'placeholder':
                    if m.op == 'call_module' or m.op == 'call_method' or  m.op == 'placeholder':
                        # TODO
                        #if m.op == "placeholder" and k is not None:
                        #    t = self.get_destination4(k)
                        #    print(f" k:{k.name} >>>> t : {t.name}")
                        #    lst_.append(t)
                        #    k = None
                        #    
                        #else:
                        #    lst_.append(m)
                        #    k = m
                        ##set_.add(m)
                        lst_.append(m)
                        break

                    if m.op == 'call_function':
                        self.get_destination(m.all_input_nodes, lst_)
                    #    #self.get_destination(m.all_input_nodes, set_)

    # TODO
    def receive_data(self, from_rank):
        ds_type = torch.tensor([0], dtype=torch.long)
        dist.recv(ds_type, from_rank)

        ds_type = self.ds_id2type[ds_type.item()]

        if ds_type is Tensor:
            return self.receive_tensor(from_rank)
        elif ds_type is tuple:
            return self.receive_tuple(from_rank)
        elif ds_type is list:
            return self.receive_list(from_rank)
        elif ds_type is Size:
            return self.receive_size(from_rank)
        elif ds_type is int:
            return self.receive_int(from_rank)
        elif ds_type is set:
            return self.receive_set(from_rank)
        else:
            print(f"#### receive_data: not supported type!")
        # TODO

    # TODO
    def send_data(self, obj, to_rank):
        ds_type = self.ds_type2id[type(obj)]
        ds_type = torch.tensor(ds_type, dtype=torch.long)
        dist.send(ds_type, to_rank)

        if isinstance(obj, torch.Tensor):
            self.send_tensor(obj, to_rank)
        elif isinstance(obj, tuple):
            self.send_tuple(obj, to_rank)
        elif isinstance(obj, list):
            self.send_list(obj, to_rank)
        elif isinstance(obj, Size):
            self.send_size(obj, to_rank)
        elif isinstance(obj, int):
            self.send_int(obj, to_rank)
        elif isinstance(obj, set):
            self.send_set(obj, to_rank)
        else:
            print(f"#### send_data: not supported type!")
        # TODO

    def receive_set(self, from_rank):
        return set(self.receive_list(from_rank))

    def send_set(self, obj, to_rank):
        self.send_list(list(obj), to_rank)

    def receive_int(self, from_rank):
        int_data = torch.tensor([0], dtype=torch.long)
        dist.recv(int_data, from_rank)
        return int_data.item()

    def send_int(self, obj, to_rank):
        int_data = torch.tensor([obj], dtype=torch.long) # ex. 2
        dist.send(int_data, to_rank)

    def receive_size(self, from_rank):
        return Size(self.receive_list(from_rank))

    def send_size(self, obj, to_rank):
        self.send_list(list(obj), to_rank)

    def receive_tuple(self, from_rank):
        return tuple(self.receive_list(from_rank))

    def send_tuple(self, obj, to_rank):
        self.send_list(list(obj), to_rank)

    def receive_tensor(self, from_rank):
        dimension = torch.tensor([0], dtype=torch.long)
        dist.recv(dimension, from_rank)
        #print(f" >>>>> recv_tensor, dimension:{dimension} from rank:{from_rank}")

        shape = torch.tensor([0] * dimension.item(), dtype=torch.long)
        dist.recv(shape, from_rank)
        #print(f" >>>>> recv_tensor, shaple:{shape} from rank:{from_rank}")
        shape = tuple(shape.tolist())

        ttype = torch.tensor([0], dtype=torch.long)
        dist.recv(ttype, from_rank)
        #print(f" >>>>> recv_tensor, ttype:{ttype} from rank:{from_rank}")

        ttype = self.tensor_id2type[ttype.item()]

        obj = torch.zeros(size=shape, dtype=ttype)
        dist.recv(obj, from_rank)
        #print(f" >>>>> recv_tensor, obj:{obj} from rank:{from_rank}")

        return obj

    def send_tensor(self, obj, to_rank):
        if isinstance(obj, torch.Tensor):
            obj_size = obj.size()
            dimension = torch.tensor(len(obj_size), dtype=torch.long) # ex. 2
            #print(f" >>>>> send_tensor, obj.size():{obj_size}, len:{len(obj_size)}, dimension:{dimension}")
        else:
            # TO DELETE
            dimension = torch.tensor(len(obj), dtype=torch.long) # ex. 2
            #print(f" >>>> send_tensor, len:{len(obj)}, dimension:{dimension}")
        dist.send(dimension, to_rank)

        if isinstance(obj, torch.Tensor):
            shape = torch.tensor(list(obj_size), dtype=torch.long) # ex. [54, 5120]
        else:
            # TO DELETE
            shape = torch.tensor(list(obj), dtype=torch.long) # ex. [54, 5120]
        #print(f" >>>>> send_tensor, shape:{shape}")
        dist.send(shape, to_rank)

        ttype = self.tensor_type2id[obj.dtype]
        ttype = torch.tensor(ttype, dtype=torch.long)
        dist.send(ttype, to_rank)
        #print(f" >>>>> send_tensor, ttype:{ttype}")

        if not obj.is_contiguous():
            obj = obj.contiguous()
            #print(f" >>> obj made to be contiguous")

        dist.send(obj, to_rank)
        #print(f" >>>>> send_tensor, obj:{obj}")

    def receive_list(self, from_rank):
        length = torch.tensor([0], dtype=torch.long)
        dist.recv(length, from_rank)

        obj = []
        for _ in range(length.item()):
            n = self.receive_data(from_rank) # TODO
            obj.append(n)

        return obj

    def send_list(self, obj, to_rank):
        length = torch.tensor(len(obj), dtype=torch.long)
        dist.send(length, to_rank)

        for n in obj:
            self.send_data(n, to_rank) # TODO



    def fx_forward4(self, *args):
        #print(f" -----> rank{self.rank}: in fx_forward4, args[0]:{args[0]}")
        self.args_iter = iter(args)

        if self.rank == 0:
            for n in self.mod.graph.nodes:
                if n.op == 'placeholder' and self.stage == 0:
                    input = next(self.args_iter)

                    #print(f">>>>> input:{input}, mbsize:{self.mbsize}")

                    if isinstance(input, torch.Tensor):
                        mbatches = torch.chunk(input, self.mbsize)
                        if self.mbsize == 1:
                            self.env2[0]["placeholder"] = input
                        else:
                            for j in range(self.mbsize):
                                self.env2[j]["placeholder"] = mbatches[j]
                    else:
                        print(f"### input:{input} not Tensor --> currently not supported!!")
                        sys.exit(1)
                    break

        #print(f" * rank:{self.rank}, in run_micro_batch_forward() ..")
        for i in range(self.mbsize):
            result = self.fx_micro_forward(i)
            next(result)

    def get_last_module(self):
        if self.rank == self.world_size - 1:
            from_, to_ = self.get_range(self.rank, self.graph)

            cur = to_
            while cur != from_:
                if cur.op == 'call_module' and cur.target != 'loss_fn':
                    print(f"[Rank:{self.rank}] ==> got last module: {cur.name}")
                    return cur.name

                cur = cur._prev



    def make_output(self):
        output = None
        if self.rank ==  self.world_size - 1:
            #target = "output"
            target = self.get_last_module()
            outputs = tuple(mb[target] for mb in self.env2) 
            print(f" ---> RANK: {self.rank},  outputs = {outputs}, type(output):{type(outputs)}")
            output = torch.cat(outputs)

        return output


    def fx_micro_forward(self, mb_idx):

        from_, to_ = self.get_range(self.rank, self.graph)
        #print(f"## rank:{self.rank}, mb_idx:{mb_idx} world_size:{self.world_size}, from_:{from_.name}, to_:{to_.name}")


        if self.rank > 0:
            target_node_name = from_._prev.name
            pre_split_rank = self.rank - 1
            #print(f"## rank:{self.rank}, receive activation from {pre_split_rank}, target_node_name:{target_node_name}")
            self.env2[mb_idx][target_node_name] = self.receive_data(pre_split_rank)


        cur = from_
        while cur != to_:
            self.fx_ir_run_node2(cur, mb_idx)
            cur = cur._next
        result = self.fx_ir_run_node2(cur, mb_idx)

        #print(f" rank:{self.rank}, cur.node name{cur.name}, target_node_name:{to_.name}")

        if self.rank < self.world_size - 1:
            target_node_name = to_.name
            next_split_rank = self.rank + 1
            #print(f"### rank:{self.rank} send activation to {next_split_rank}, target_node_name:{target_node_name}")
            obj = self.env2[mb_idx][target_node_name]
            self.send_data(obj, next_split_rank)

        yield result



    #def restore_env(self, node: Node) -> Tuple[Tuple, Dict]:
    #    #print(f"## before restore_env, node:{node}, node.args:{node.args}, node.kwargs:{node.kwargs}")
    #
    #    args = fx.graph.map_arg(node.args, lambda n: self.env[n.name])
    #    assert isinstance(args, tuple)
    #
    #    kwargs = fx.graph.map_arg(node.kwargs, lambda n: self.env[n.name])
    #    assert isinstance(kwargs, dict)
    #
    #    #print(f">>> after restore_env, node:{node}, node.name:{node.name}, args:{args}, kwargs:{kwargs}")
    #
    #    return args, kwargs
        

    def fx_ir_run_node2(self, node, mb_idx):

        #args, kwargs = self.restore_env(node)

        result = Any

        #if node.op == 'placeholder' and self.stage == 0:
        if node.op == 'placeholder' and node.name == 'x' and self.stage == 0:
            #result = next(self.args_iter)
            result = self.env2[mb_idx]["placeholder"]
        
        elif node.op == 'placeholder' and node.name == 'targets' and self.stage > 0:
            result = self.env2[mb_idx]["targets"]

        elif node.op == 'get_attr':
            target_atoms = node.target.split('.')
            attr_itr = self.mod
            for i , atom in enumerate(target_atoms):
                if not hasattr(attr_itr, atom):
                    raise RuntimeError(\
                            f"Node referenced nonexistant target{'.'.join(target_atoms[:i])}")
                attr_itr = getattr(attr_itr, atom)
            result = attr_itr

        elif node.op == 'call_function':
            result = node.target(\
                    *fx.graph.map_arg(node.args, lambda n: self.env2[mb_idx][n.name]), \
                    **fx.graph.map_arg(node.kwargs, lambda n: self.env2[mb_idx][n.name]))
            #flat_args = []
            #def extract_tensor_args(b):
            #    a = self.env2[mb_idx][b.name]
            #    nonlocal flat_args
            #    if isinstance(a, torch.Tensor):
            #        val = a.detach().requires_grad_(a.requires_grad)
            #        flat_args.append(val)
            #        # DEBUG
            #        #print(f" >>>>>>>>>>>> call_function[node.name={node.name}] a is Tensor:{a}")
            #        return val
            #    else:
            #        flat_args.append(a)
            #        #print(f" >>>>>>>>>>>>>> call_function[node.name={node.name}] a is not Tensor:{a}")
            #        return a
            #    return a
            #
            #args = fx.graph.map_arg(node.args, extract_tensor_args)
            #kwargs = fx.graph.map_arg(node.kwargs, extract_tensor_args)
            #
            ## DEBUG
            ##print(f" --> call_function[node.name:{node.name}:  args:{args}, kwargs:{kwargs}")
            #result = node.target(*args, **kwargs)
            #
            #self.fwd_cache2[mb_idx][node.name] = \
            #        ( result if isinstance(result, tuple) else (result,), \
            #        flat_args, )
            ##print(f" --> call_function:  result:[{type(result)}], flat_args:{type(flat_args)}")

        elif node.op == 'call_method':
            #self_obj, *args = fx.graph.map_arg(node.args, lambda n: self.env2[mb_idx][n.name])
            #kwargs = fx.graph.map_arg(node.kwargs, lambda n: self.env2[mb_idx][n.name])
            #result = getattr(self_obj, node.target)(*args, **kwargs)
            arg0_b = node.args[0]
            arg0_a = self.env2[mb_idx][arg0_b.name]
            if isinstance(arg0_a, torch.Tensor):
                self_obj = arg0_a.detach().requires_grad_(arg0_a.requires_grad)
            else:
                self_obj = arg0_a

            flat_args = [self_obj, ]

            def extract_tensor_args(b):
                a = self.env2[mb_idx][b.name]
                nonlocal flat_args
                if isinstance(a, torch.Tensor):
                    val = a.detach().requires_grad_(a.requires_grad)
                    flat_args.append(val)
                    return val
                else:
                    flat_args.append(a)
                    return a

                return a

            args = fx.graph.map_arg(node.args[1:], extract_tensor_args)
            kwargs = fx.graph.map_arg(node.kwargs, extract_tensor_args)

            result = getattr(self_obj, node.target)(*args, **kwargs)

            self.fwd_cache2[mb_idx][node.name] = \
                    ( result if isinstance(result, tuple) else (result,), \
                    flat_args, )
            #print(f" --> call_method:  result:[{type(result)}], flat_args:{type(flat_args)}")

        elif node.op == 'call_module':
            #result = self.modules[node.target](\
            #        *fx.graph.map_arg(node.args, lambda n: self.env2[mb_idx][n.name]),\
            #        **fx.graph.map_arg(node.kwargs, lambda n: self.env2[mb_idx][n.name]))
            flat_args = []
            def extract_tensor_args(b):
                a = self.env2[mb_idx][b.name]
                nonlocal flat_args
                # TODO
                #if isinstance(a, torch.Tensor):
                    #val = a.detach().requires_grad_(a.requires_grad)
                if isinstance(a, torch.Tensor) and a.is_floating_point():
                    val = a.detach().requires_grad_(True) 
                    flat_args.append(val)
                    return val
                else:
                    flat_args.append(a)
                    return a

                return a

            args = fx.graph.map_arg(node.args, extract_tensor_args)
            kwargs = fx.graph.map_arg(node.kwargs, extract_tensor_args)

            target_atoms = node.target.split('.')
            attr_itr = self.mod
            for i , atom in enumerate(target_atoms):
                if not hasattr(attr_itr, atom):
                    raise RuntimeError(\
                            f"Node referenced nonexistant target{'.'.join(target_atoms[:i])}")
                attr_itr = getattr(attr_itr, atom)
            submod = attr_itr

            if node.target == 'loss_fn':
                myargs = [None, None]
                myargs[0] = args[0].reshape(-1, ntokens)
                myargs[1] = args[1]
                myargs = tuple(myargs)

                result = submod(*myargs, **kwargs)
                #print(f" In forward --> node.target=='loss_fn' ==> result:{result}")
            else:
                result = submod(*args, **kwargs)

            if node.target == 'loss_fn':
                #print(f" node.target == 'loss_fn' --> {self.env2[mb_idx][str(node.all_input_nodes[0])]}")
                if not str(node.all_input_nodes[0]).startswith("target"):
                    self.output = self.env2[mb_idx][str(node.all_input_nodes[0])]
                self.grads2[mb_idx][node.name] = (None,)

            self.fwd_cache2[mb_idx][node.name] = \
                    ( result if isinstance(result, tuple) else (result,), \
                    flat_args, )

            if node.target == 'loss_fn':
                self.loss[mb_idx] = result
            #print(f" --> call_module:  node.name:{node.name}, fwd_cache2[{mb_idx}][{node.name}] set !!")

        elif node.op == 'output':
            result = fx.graph.map_arg(node.args[0], lambda n: self.env2[mb_idx][n.name])

        self.env2[mb_idx][node.name] = result

        #print(f" ## run [rank:{self.rank}, micro#:{mb_idx}] - node:{node.name}, node.op:{node.op}")

        return result


    def fx_backward4(self, *args):
        #print(f" -----> rank{self.rank}: in fx_backward4, args[0]:{args[0]}")

        for i in range(self.mbsize):
            result = self.fx_micro_backward(i)
            next(result)


    def fx_micro_backward(self, mb_idx):

        from_, to_ = self.get_range(self.rank, self.graph)
    
        if self.rank < self.world_size - 1:
            #target_node_name = to_._next.name # TODO
            target_node_name = to_.name # TODO
            pre_split_rank = self.rank + 1
            #print(f"## rank:{self.rank}, receive grads from {pre_split_rank}, target_node_name:{target_node_name}")
            self.grads2[mb_idx][target_node_name] = self.receive_data(pre_split_rank)
    
        node = to_
        while node != from_:


            if node.op == 'output':
                node = node._prev
                continue
    
            if node.op == 'call_module' or node.op == 'call_method':
            #if node.op == 'call_module' or node.op == 'call_method' or node.op == 'call_function':
    
                def extract_tensor_args(b):
                    a = self.env2[mb_idx][b.name]
                    # TODO
                    #if isinstance(a, torch.Tensor) and a.is_floating_point():
                    #    val = a.detach().requires_grad_(True) 
                    if isinstance(a, torch.Tensor):
                        val = a.detach().requires_grad_(a.requires_grad)
                        return val
                    else:
                        return a
    
                args = ()
                kwargs = fx.graph.map_arg(node.kwargs, extract_tensor_args)
    
                kwargs = dict(kwargs)
                k1, k2 = self.fwd_cache2[mb_idx].pop(node.name)

                kwargs["stage_output"] = k1
                kwargs["input_values"] = k2
    
                kwargs["output_grads"] = self.grads2[mb_idx][node.name]
                #kwargs["outputs_with_grads_idxs"] = [0]
                if isinstance(k1, tuple):
                    num_nodes = len(k1)
                    if num_nodes > 1:
                        print(f" ## num_nodes: {num_nodes} ##")
                else:
                    num_nodes = 1
                kwargs["outputs_with_grads_idxs"] = [i for i in range(num_nodes)]

                if node.target != 'loss_fn' and self.grads2[mb_idx][node.name] == (None,):
                    node = node._prev
                    continue
    
                result = stage_backward(*args, **kwargs)
    
                next_ = []
                self.get_destination(node.all_input_nodes, next_)
    
                cnt = len(result[0])
    
                for i, m in enumerate(next_):
                    if cnt > 1:
                        if isinstance(result[0][i], list) and result[0][i] != None:
                            self.grads2[mb_idx][m.name] = torch.stack(result[0][i], 0)
                            #print(f" ## fx_micro_backward, cnt:{cnt} node.name:{node.name}, mb_idx:{mb_idx}, m.name:{m.name}, result <= {self.grads2[mb_idx][m.name]}") # DEBUG

                            #print(f" 1############ fx_micro_backward, node.name:{node.name}, mb_idx:{mb_idx}, m.name:{m.name}, self.grads2[{mb_idx}][{m.name}] set") # DEBUG
                        else:
                            self.grads2[mb_idx][m.name] = ((result[0][i], ) if not isinstance(result[0][i], tuple) else result[0][i])
                            #print(f" #### fx_micro_backward, cnt:{cnt} node.name:{node.name}, mb_idx:{mb_idx}, m.name:{m.name}, result <= {self.grads2[mb_idx][m.name]}") # DEBUG
                            
                            #print(f" 2############ fx_micro_backward, node.name:{node.name}, mb_idx:{mb_idx}, m.name:{m.name}, self.grads2[{mb_idx}][{m.name}] set") # DEBUG
                    else:
                        if isinstance(result[0], list) and result[0][0] != None:
                            self.grads2[mb_idx][m.name] = torch.stack(result[0], 0)
                            #print(f" ######## fx_micro_backward, cnt:{cnt} node.name:{node.name}, mb_idx:{mb_idx}, m.name:{m.name}, result <= {self.grads2[mb_idx][m.name]}") # DEBUG

                            #print(f" 3############ fx_micro_backward, node.name:{node.name}, mb_idx:{mb_idx}, m.name:{m.name}, self.grads2[{mb_idx}][{m.name}] set") # DEBUG
                        else:
                            self.grads2[mb_idx][m.name] = ((result[0], ) if not isinstance(result[0], tuple) else result[0])
                            #print(f" ############ fx_micro_backward, cnt:{cnt} node.name:{node.name}, mb_idx:{mb_idx}, m.name:{m.name}, result <= {self.grads2[mb_idx][m.name]}") # DEBUG

                            #print(f" 4############ fx_micro_backward, node.name:{node.name}, mb_idx:{mb_idx}, m.name:{m.name}, self.grads2[{mb_idx}][{m.name}] set") # DEBUG
                node = node._prev
                continue

            if node.op == 'placeholder' and node.target == 'targets':
                node = node._prev
                continue
    
            node = node._prev
    
        if self.rank > 0:
            #target_node_name = str(node.target)
            target_node_name = node.name
            next_split_rank = self.rank - 1
            #print(f" ---- fx_backward: got {target_node_name}'s grads --> to be sent to rank:{next_split_rank}") # DEBUG
            obj = self.grads2[mb_idx][target_node_name]
            self.send_data(obj, next_split_rank)

        yield 0

train_iter = WikiText2(split='train')
tokenizer = get_tokenizer('basic_english')
vocab = build_vocab_from_iterator(map(tokenizer, train_iter), specials=["<unk>"])
vocab.set_default_index(vocab["<unk>"]) 

def data_process(raw_text_iter):
  data = [torch.tensor(vocab(tokenizer(item)), dtype=torch.long) for item in raw_text_iter]
  return torch.cat(tuple(filter(lambda t: t.numel() > 0, data)))

# TODO
#if int(os.environ["RANK"]) == 0:
#    train_iter, val_iter, test_iter = WikiText2()
#    train_data = data_process(train_iter)
#    val_data = data_process(val_iter)
#    test_data = data_process(test_iter)
train_iter, val_iter, test_iter = WikiText2()
train_data = data_process(train_iter)
val_data = data_process(val_iter)
test_data = data_process(test_iter)

#device = torch.device("cuda")
device = torch.device("cpu")

def batchify(data, bsz):
    # Divide the dataset into bsz parts.
    nbatch = data.size(0) // bsz
    # Trim off any extra elements that wouldn't cleanly fit (remainders).
    data = data.narrow(0, 0, nbatch * bsz)
    # Evenly divide the data across the bsz batches.
    data = data.view(bsz, -1).t().contiguous()
    return data.to(device)

batch_size = 20
eval_batch_size = 10


if int(os.environ["RANK"]) == 0:
    train_data = batchify(train_data, batch_size)
    val_data = batchify(val_data, eval_batch_size)
    test_data = batchify(test_data, eval_batch_size)


bptt = 25
def get_batch(source, i):
    seq_len = min(bptt, len(source) - 1 - i)
    data = source[i:i+seq_len]
    target = source[i+1:i+1+seq_len].view(-1)
    # Need batch dimension first for pipeline parallelism.
    return data.t(), target


ntokens = len(vocab) # the size of vocabulary
emsize = 4096 # embedding dimension
nhid = 4096 # the dimension of the feedforward network model in nn.TransformerEncoder
nlayers = 12 # the number of nn.TransformerEncoderLayer in nn.TransformerEncoder
nhead = 16 # the number of heads in the multiheadattention models
dropout = 0.2 # the dropout value


# Add encoder in the beginning.
tmp_list = [Encoder(ntokens, emsize, dropout).to(device)]
module_list = []

# Add all the necessary transformer blocks.
for i in range(nlayers):
    transformer_block = TransformerEncoderLayer(emsize, nhead, nhid, dropout)
    tmp_list.append(transformer_block.to(device))

# Add decoder in the end.
tmp_list.append(Decoder(ntokens, emsize).to(device))
module_list.append(nn.Sequential(*tmp_list))

model = torch.nn.Sequential(*module_list)


#####
sim_split = Simple_split_test()
sim_split.metadata_transfer()


# TEST ONLY
micro_batch_size = num_rank // 2
#fx_run2 = FXRun2(sim_split, sim_split.device, mbsize=micro_batch_size)
fx_run2 = FXRun2(sim_split, sim_split.device, sim_split.train_data_size, mbsize=micro_batch_size) # TODO
print(f">>> micro batch size = {fx_run2.mbsize}")

fx_run2.print_range()

if sim_split.rank == 0:
    print ('Total parameters in model: {:,}'.format(get_total_params(model)))


#fx_run2.mod.train()
lr = 5.0
optimizer1 = torch.optim.SGD(fx_run2.mod.parameters(), lr=lr)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer1, 1.0, gamma=0.95)


def train():
    fx_run2.mod.train() # Turn on the train mode
    total_loss = 0.
    start_time = time.time()
    ntokens = len(vocab)

    # Train only for 50 batches to keep script execution time low.
    #nbatches = min(50 * bptt, train_data.size(0) - 1)
    nbatches = min(50 * bptt, fx_run2.train_data_size - 1) 

    for batch, i in enumerate(range(0, nbatches, bptt)):

        data = None
        targets = None

        if fx_run2.rank == 0:

            # move data to first host
            # move targets to last host

            data, targets = get_batch(train_data, i)

            target_node_name = "targets"
            mbatches = torch.chunk(targets, fx_run2.mbsize)
            if fx_run2.mbsize == 1:
                fx_run2.env2[0][target_node_name] = targets
            else:
                for j in range(fx_run2.mbsize):
                    fx_run2.env2[j][target_node_name] = mbatches[j]

            for j in range(fx_run2.mbsize):
                obj = fx_run2.env2[j][target_node_name]
                fx_run2.send_data(obj, fx_run2.world_size - 1)
                #print(f">>>> [rank:0] sent [j:{j}] ==> {targets}")
            

        if fx_run2.rank == fx_run2.world_size - 1:
            #print(f" << RANK:{fx_run2.rank},  WORLD_SIZE:{fx_run2.world_size}, mbsize:{fx_run2.mbsize}")
            target_node_name = "targets"
            for j in range(fx_run2.mbsize):
                fx_run2.env2[j][target_node_name] = fx_run2.receive_data(0)
                #print(f">>>> received <==== env2[{j}][{target_node_name}]: {fx_run2.env2[j][target_node_name]}")
            if fx_run2.mbsize == 1:
                targets = fx_run2.env2[0][target_node_name]
            else:
                outputs = tuple(mb["targets"] for mb in fx_run2.env2)
                targets = torch.cat(outputs)


        optimizer1.zero_grad()

        output1 = fx_run2.fx_forward4(data, targets)
        loss1 = fx_run2.loss

        fx_run2.fx_backward4(loss1)

        torch.nn.utils.clip_grad_norm_(fx_run2.mod.parameters(), 0.5)
        optimizer1.step()

        if sim_split.rank == sim_split.world_size - 1:
            #total_loss += loss1.item()
            loss =  sum(loss1) / fx_run2.mbsize
            total_loss += loss
            log_interval = 10
            if batch % log_interval == 0 and batch > 0:
                cur_loss = total_loss / log_interval
                elapsed = time.time() - start_time
                print('| epoch {:3d} | {:5d}/{:5d} batches | '
                    'lr {:02.2f} | ms/batch {:5.2f} | '
                    'loss {:5.2f} | ppl {:8.2f}'.format(
                        epoch, batch, nbatches // bptt, scheduler.get_lr()[0],
                        elapsed * 1000 / log_interval,
                        cur_loss, math.exp(cur_loss)))

                total_loss = 0
                start_time = time.time()

best_val_loss = float("inf")
epochs = 5 # The number of epochs
best_model = None

if sim_split.rank == 0:
    tick = time.time()

for epoch in range(1, epochs + 1):
    epoch_start_time = time.time()
    train()
    scheduler.step()


if sim_split.rank == 0:
    tock=time.time()
    elapsed_time = tock - tick

    print('Time elapsed: %.3f sec ' % (elapsed_time))

if sim_split.rank == sim_split.world_size - 1:
    print(f"RANK:{sim_split.rank} ###################### output #############")
    output1 = fx_run2.make_output()
    print(output1)
    print(f"###################################")

print(f"[rank:{sim_split.rank}, run completed ...")

#rpc.shutdown()

