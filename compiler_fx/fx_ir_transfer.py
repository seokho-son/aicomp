#
# Copyright (c) 2023-present, ETRI, All rights reserved.
#
#
#  This is a PoC that transfers a partition of the FX IR generated by FX compile to another machine
#
#
#  Sample Usage:
#      <machine #0>
#            torchrun --nproc_per_node=2 --nnodes=2 --node_rank=0
#                  --master_addr="X.X.X.X" --master_port=29500 fx_ir_transfer.py
#      <machine #1>
#            torchrun --nproc_per_node=2 --nnodes=2 --node_rank=1
#                  --master_addr="X.X.X.X" --master_port=29500 fx_ir_ransfer.py
#


import torch
from torch import Tensor
from torch.nn.parameter import Parameter, UninitializedParameter
from torch.nn import init
import torch.nn as nn
from torch.optim import Adam
from torch import fx
from torch.fx.node import Node
import time

import torch.distributed as dist
import datetime
import torch.distributed.rpc as rpc

from time import sleep

from torch.fx.graph_module import GraphModule
from torch.fx.passes.split_module import split_module


import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))

torch.manual_seed(42)

batch_size = 64
in_features = 5120
out_features = 5120
hidden = 5120

# N: the number of HOSTs
N = 4


class TestModel(nn.Module):
    def __init__(self):
        super().__init__()

        self.linear1 = nn.Linear(in_features, hidden)
        self.linear2 = nn.ModuleList()
        for i in range(2):
            self.linear2.append(nn.Linear(hidden, hidden))

        self.linear3 = nn.ModuleList()
        for i in range(2):
            self.linear3.append(nn.Linear(hidden, hidden))

        self.linear4 = nn.ModuleList()
        for i in range(2):
            self.linear4.append(nn.Linear(hidden, hidden))

        self.linear5 = nn.ModuleList()
        for i in range(2):
            self.linear5.append(nn.Linear(hidden, hidden))
        self.linear6 = nn.Linear(hidden, out_features)
        self.relu = nn.ReLU(inplace = True)

    def forward(self, x):
        x = self.relu(self.linear1(x))
        for m in self.linear2:
            x = self.relu(m(x))
        for m in self.linear3:
            x = self.relu(m(x))
        for m in self.linear4:
            x = self.relu(m(x))
        for m in self.linear5:
            x = self.relu(m(x))
        x = self.linear6(x)
        x = self.relu(x)
        return x

#t1 = TestModel()

#
#print(t1)

#gm = fx.symbolic_trace(t1)


# TODO
class Communication(object):
    def __init__(self):
        self.initialize_comm()

    def initialize_comm(self):

        if dist.is_initialized():
            print(f"Communication already initialized")
            return


        self.rank = int(os.environ["RANK"])
        self.world_size = int(os.environ["WORLD_SIZE"])
        self.master_addr = os.getenv("MASTER_ADDR")
        self.master_port = os.getenv("MASTER_PORT")

        #
        print(f" --- rank:{self.rank}, world_size:{self.world_size}, master:{self.master_addr}, port:{self.master_port}")

        self.backend = "gloo"
        init_method = "tcp://" + str(self.master_addr) + ":" + str(self.master_port)

        dist.init_process_group(backend=self.backend, rank=self.rank, world_size=self.world_size, init_method=init_method)

        #
        print(f" --- rank:{dist.get_rank()}, world_size:{dist.get_world_size()}")

        options = rpc.TensorPipeRpcBackendOptions(num_worker_threads=10, rpc_timeout=30)

        rpc.init_rpc(f"worker{self.rank}", rank=self.rank, world_size=self.world_size, rpc_backend_options=options,)

        # rpc.shutdown()


    def simple_split(self, gm, t1, metadata_range):
        length = gm.graph.nodes.__len__()
        segment = length // N
        print(f"segment ==> {segment}")


        def part_fn(node):

            last_idx, last_name = metadata_range[-1]

            idx = 0

            cur = node
            while cur.name != last_name:
                for i, m_name in metadata_range:
                    if cur.name == m_name:
                        idx = i
                        #print(f" part_fn:  node.name:{node.name}, m_name:{m_name}, --> {idx}")
                        return idx

                cur = cur._next

            if cur.name == last_name:
                idx = last_idx
            #print(f" part_fn:  node.name:{node.name}, --> {idx}")
            return idx


        k, cnt = 0, 0
        for n in gm.graph.nodes:
            if n.op == 'call_module':
                cnt = cnt + 1

            if cnt == segment:
                metadata_range.append((k, n.name))
                k = k + 1
                cnt = 0

        print(metadata_range)

        submodules = split_module(gm, t1, part_fn, keep_original_order=True)
        #print(submodules)

        return submodules

    def setup_pair_info(self):

        device = torch.device("cpu")

        if self.rank == 0:
            self.rank_pair: Dict[int, List[int]] = {}
            rank_pair_obj = [self.rank_pair]

            for rank in range(self.world_size):
                #self.rank_pair.setdefault(self.rank, [0, self.rank] )
                if rank == 0:
                    continue
                self.rank_pair.setdefault(rank, [0, rank] )

            dist.broadcast_object_list(rank_pair_obj, src=0, device=device)
        else:
            self.rank_pair: Dict[int, List[int]] = {}

            rank_pair_obj = [None]

            dist.broadcast_object_list(rank_pair_obj, src=0, device=device)
            self.rank_pair = rank_pair_obj[0]

        print(f"## setup_pair_info: rank:{self.rank}, rank_pair:{self.rank_pair}")


    def setup_ctrl_group(self):
        self.ctrl_group: Dict[int, Any] = {}

        for rank in range(self.world_size):
            if rank == 0:
                continue
            pair_ranks = self.rank_pair[rank]
            self.ctrl_group[rank] = dist.new_group(pair_ranks)
        print(f"## setup_ctrl_group completed.")


    def transfer_test(self):

        self.metadata_range = []

        if self.rank == 0:
            t1 = TestModel()
            gm = fx.symbolic_trace(t1)
            device = torch.device("cpu")

            self.setup_pair_info()
            self.setup_ctrl_group()

            #
            submods = self.simple_split(gm, t1, self.metadata_range)

            skip = False
            to_rank = 0
            for submod in submods.modules():
                if skip == False and isinstance(submod, fx.GraphModule):
                    skip = True
                    continue
                if skip == True and isinstance(submod, fx.GraphModule):
                    print(f"submod:{submod._get_name()}")

                    if to_rank == 0:
                        print(f"### rank = 0")
                        for node in submod.graph.nodes:
                            print(f"-- node.op:{node.op}, node.name:{node.name}, node.target:{node.target}, node.all_input_nodes:{node.all_input_nodes}")

                    else:
                        print(f"### rank = TO: {to_rank}")
                        object_list = [submod]
                        dist.broadcast_object_list(object_list, src=0, group=self.ctrl_group[to_rank], device=device)
                    to_rank = to_rank + 1

                    print(f" >> FROM:{self.rank} ==> TO:{to_rank} FX IR partition transferred")

        else:
            device = torch.device("cpu")

            self.setup_pair_info()
            self.setup_ctrl_group()

            object_list = [None]
            dist.broadcast_object_list(object_list, src=0, group=self.ctrl_group[self.rank], device=device)

            #self.graph = object_list[0]
            submod = object_list[0]

            #if self.graph is None:
            if submod is None:
                print(f"FX IR sync failed")
            else:
                print(f" ### rank:{self.rank} <==  FX IR partition")
                #for node in self.graph.nodes:
                for node in submod.graph.nodes:
                    print(f"-- node.op:{node.op}, node.name:{node.name}, node.target:{node.target}, node.all_input_nodes:{node.all_input_nodes}")


        rpc.shutdown()


comm = Communication()

comm.transfer_test()

