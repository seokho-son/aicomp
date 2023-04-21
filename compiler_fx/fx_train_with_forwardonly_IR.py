#
# Copyright (c) 2023-present, ETRI, All rights reserved.
#
#
# This is a PoC that trains a model using IR generated by FX compile.
#   : Forward and Backward using only forward-IR
#



import torch
from torch import Tensor
from torch.nn.parameter import Parameter, UninitializedParameter
from torch.nn import init
import torch.nn as nn
from torch.optim import Adam
from torch import fx
from torch.fx.node import Node
import copy
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import time


import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))

torch.manual_seed(42)

batch_size = 64
in_features = 5120
out_features = 5120
hidden = 5120


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
        #self.relu = nn.ReLU(inplace = True)
        self.relu = nn.ReLU(inplace = False)

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

t1 = TestModel()

t2 = copy.deepcopy(t1)

#
print(t1)
print("-----------------------")
#print(t2)

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

loss_fn = torch.nn.MSELoss()
wrapper = SimpleLossWrapper(t1, loss_fn)

#gm1 = fx.symbolic_trace(t1)
gm1 = fx.symbolic_trace(wrapper)

for node in gm1.graph.nodes:
    print(f"node.op:{node.op}, node.name:{node.name}, node.target:{node.target}, node.all_input_nodes:{node.all_input_nodes}")

print("-------------")
print(gm1.code)
print("-------------")

# _get_loss_output: adapted from PiPPy
def _get_loss_output(graph: fx.Graph):
     output_nodes = [n for n in graph.nodes if n.op == 'output']
     assert len(output_nodes) == 1
     output_node = output_nodes[0]
     loss_node = output_node.args[0]

     return loss_node, output_node

loss_node, output_node = _get_loss_output(gm1.graph)

#print(loss_node)
#print(output_node)


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
            grad_inputs.append(val.grad)
        else:
            grad_inputs.append(None)

        barrier_token = None
    return grad_inputs, barrier_token



class FXRun:

    def __init__(self, mod):
        self.mod = mod
        self.graph = mod.graph
        self.modules = dict(self.mod.named_modules())

        self.loss = None

        self.env: Dict[str, Node] = {}
        #self.env: Dict[Node, Any] = {}

        # TODO
        #self.fwd_cache: Dict[int, Tuple[Any, List[torch.Tensor]]] = {}
        self.fwd_cache: Dict[str, Tuple[Any, List[torch.Tensor]]] = {}
        self.grads: Dict[str, Any] = {}

    def get_destination(self, input_nodes, set_):
        for i, m in enumerate(input_nodes):
            for n in self.graph.nodes:
                if n.name == m.name:
                    if m.op == 'call_module' or m.op == 'call_method':
                        set_.add(m)
                        break

                    if m.op == 'call_function':
                        self.get_destination(m.all_input_nodes, set_)


    def fx_forward(self, *args):
        args_iter = iter(args)

        for node in self.graph.nodes:
            if node.op == 'placeholder':
                result = next(args_iter)
                #print(f"placeholder: node.name:{node.name}, result:{result}")

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
                        *fx.graph.map_arg(node.args, lambda n: self.env[n.name]), \
                        **fx.graph.map_arg(node.kwargs, lambda n: self.env[n.name]))

            elif node.op == 'call_method':
                #self_obj, *args = fx.graph.map_arg(node.args, lambda n: self.env[n.name])
                #kwargs = fx.graph.map_arg(node.kwargs, lambda n: self.env[n.name])
                #result = getattr(self_obj, node.target)(*args, **kwargs)

                arg0_b = node.args[0]

                arg0_a = self.env[arg0_b.name]
                self_obj = arg0_a.detach().requires_grad_(arg0_a.requires_grad)

                flat_args = [self_obj, ]

                def extract_tensor_args(b):
                    a = self.env[b.name]
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

                self.fwd_cache[node.name] = \
                        ( result if isinstance(result, tuple) else (result,), \
                        flat_args, )


            elif node.op == 'call_module':
                #result = self.modules[node.target](\
                #        *fx.graph.map_arg(node.args, lambda n: self.env[n.name]),\
                #        **fx.graph.map_arg(node.kwargs, lambda n: self.env[n.name]))

                flat_args = []
                def extract_tensor_args(b):
                    a = self.env[b.name]
                    nonlocal flat_args
                    if isinstance(a, torch.Tensor):
                        val = a.detach().requires_grad_(a.requires_grad)
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
                result = submod(*args, **kwargs)


                if node.target == 'loss_fn':
                    if not str(node.all_input_nodes[0]).startswith("target"):
                        self.output = self.env[str(node.all_input_nodes[0])]
                    self.grads[node.name] = (None,)

                self.fwd_cache[node.name] = \
                        ( result if isinstance(result, tuple) else (result,), \
                        flat_args, )

                if node.target == 'loss_fn':
                    self.loss = result

            self.env[node.name] = result

            # 'output' excluded when LossWrapper applied

        #return fx.graph.map_arg(self.env[node.name], lambda n: self.env[n.name])
        return self.output


    def fx_backward(self, *args):
        loss = args

        for node in reversed(self.graph.nodes):
            if node.op == 'output':
                pass

            if node.op == 'call_module' or node.op == 'call_method':

                def extract_tensor_args(b):
                    a = self.env[b.name]
                    if isinstance(a, torch.Tensor):
                        val = a.detach().requires_grad_(a.requires_grad)
                        return val
                    else:
                        return a

                #args = fx.graph.map_arg(node.args, extract_tensor_args) 
                args = ()
                kwargs = fx.graph.map_arg(node.kwargs, extract_tensor_args)

                kwargs = dict(kwargs)
                k1, k2 = self.fwd_cache.pop(node.name)

                kwargs["stage_output"] = k1
                kwargs["input_values"] = k2
                kwargs["output_grads"] = self.grads[node.name]
                kwargs["outputs_with_grads_idxs"] = [0]

                result = stage_backward(*args, **kwargs)

                #
                #self.grads[str(node.all_input_nodes[0])] = result[0]
                next_ = set([])
                self.get_destination(node.all_input_nodes, next_)

                cnt = len(result[0])
                for m in next_:
                    if cnt > 1:
                        self.grads[m.name] = tuple(result[0])
                    else:
                        self.grads[m.name] = result[0]

for node in gm1.graph.nodes:
    print(f"node.op:{node.op}, node.name:{node.name}, node.target:{node.target}")

print("-------------")
print(gm1.code)
print("-------------")


t1.train()
t2.train()
optimizer1 = Adam(t1.parameters(), lr=3e-5)
optimizer2 = Adam(t2.parameters(), lr=3e-5)

fx_run = FXRun(gm1)

tick =  time.time()

#sample_input = torch.rand(batch_size, in_features)
sample_output = torch.rand(batch_size, out_features)

for i in range(20):
    sample_input = torch.rand(batch_size, in_features)

    optimizer1.zero_grad()
    optimizer2.zero_grad()

    #loss1 = fx_run.fx_forward(sample_input, sample_output) # actual
    output1 = fx_run.fx_forward(sample_input, sample_output) # actual
    loss1 = fx_run.loss
    fx_run.fx_backward(loss1)

    #print(f"[{i}] loss1 ==> {loss1}")
    optimizer1.step()

    output2 = t2(sample_input) # expected
    loss2 = torch.nn.MSELoss()(output2, sample_output)
    loss2.backward()
    optimizer2.step()

    print(f'Step {i}, Loss1: {loss1}, Loss2: {loss2}')

    torch.testing.assert_close(output1, output2)
    #torch.allclose(output1, output2)

tock = time.time()
elapsed_time = tock - tick
print('Time elapsed: %.3f sec ' % (elapsed_time))
print(output1)
print("#######")
print(output2)
