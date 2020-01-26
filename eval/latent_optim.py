# -*- coding: utf-8 -*-
"""
Created on Fri Jul  5 14:45:02 2019

@author: jacqu

Gradient descent optimization of objective target function in latent space.
"""

import torch
import numpy as np
from numpy.linalg import norm
import pandas as pd

from rdkit import Chem
from rdkit.Chem import QED, Crippen, Descriptors

import sys
import matplotlib.pyplot as plt
import seaborn as sns

from model import Model

if(__name__=='__main__'):
    sys.path.append("data_processing")
    from rdkit_to_nx import smiles_to_nx
    from molDataset import molDataset
    from utils import *
    
    # Set to true if providing a seed compound to start optimization. Otherwise, random start in the latent space
    seed= False
    N_steps=10
    lr=0.1
    
    def eps(props):
        return (props[1]-2)**2+(props[0]-1)**2
    
    molecules =[]
    data = molDataset()
    
    if(seed):
        # Begin optimization with seed compound : a DUD decoy   
        s_seed='COc1ccc(CN(C)C(=O)C(=O)N[C@@H](C)c2ccc(F)cc2)c(O)c1'
        fig = Draw.MolToImage(Chem.MolFromSmiles(s_seed))
    
        # Pass to loader 
        data.pass_smiles_list([s_seed])
        graph,_,_,_ = data.__getitem__(0)
        send_graph_to_device(graph,model.device)
        
        # pass to model
        z = model.encode(graph, mean_only=True)
        z=z.unsqueeze(dim=0)
    else:
        print("Sampling random latent vector")
        z = model.sample_z_prior(1)
        z.requires_grad=True
    
    # ================= Optimization process ================================
    
    for i in range(N_steps):
        # Objective function 
        epsilon= eps(model.props(z)[0])
        g=torch.autograd.grad(epsilon,z)
        with torch.no_grad(): # Gradient descent, don't track
            z = z - lr*g[0]
            lr=0.9*lr
            
            out=model.decode(z)
            smi = model.probas_to_smiles(out)[0]
            print(smi)
            m=Chem.MolFromSmiles(smi)
            logP=0
            if(m!=None):
                logP= Chem.Crippen.MolLogP(m)
                qed = Chem.QED.default(m)
                print(f'predicted logP: {model.props(z)[0,1].item():.2f}, true: {logP:.2f}')
                print(f'predicted QED: {model.props(z)[0,0].item():.2f}, true: {qed:.2f}')
            else:
                print(f'predicted logP / QED : {model.props(z)[0,1].item():.2f} / {model.props(z)[0,0].item():.2f}, invalid smiles')
        z.requires_grad =True