# -*- coding: utf-8 -*-
"""
Created on Sun May 31 19:33:53 2020

@author: jacqu

Bayesian optimization following implementation by Kusner et al, in Grammar VAE 

@ https://github.com/mkusner/grammarVAE 

"""

import os, sys

script_dir = os.path.dirname(os.path.realpath(__file__))
if __name__ == "__main__":
    sys.path.append(os.path.join(script_dir, '../..'))

import pickle
import gzip
import numpy as np

import torch
import argparse

from multiprocessing import Pool

from rdkit import Chem
from rdkit.Chem import MolFromSmiles, MolToSmiles
from rdkit.Chem import Draw
from rdkit.Chem import Descriptors

import networkx as nx
from rdkit.Chem import rdmolops

import data_processing.sascorer as sascorer
import data_processing.comp_metrics

from model import model_from_json
from dataloaders.molDataset import Loader
from data_processing.comp_metrics import cycle_score, logP, qed
from data_processing.sascorer import calculateScore
from selfies import encoder,decoder
from utils import soft_mkdir

from docking.docking import dock, set_path

from sparse_gp import SparseGP
import scipy.stats as sps
from time import time

parser = argparse.ArgumentParser()

parser.add_argument('--name', type = str ,  default='benchmark') # name of model to use 

parser.add_argument('--seed', type = int ,  default=1) # random seed (simulation id for multiple runs)
parser.add_argument('--model', type = str ,  default='250k') # name of model to use 

parser.add_argument('--obj', type = str ,  default='logp') # objective : logp (composite), qed (composite), qsar or docking

parser.add_argument('--n_iters', type = int ,  default=20) # Number of iterations
parser.add_argument('--epochs', type = int ,  default=20) # Number of training epochs for gaussian process 

parser.add_argument('--bo_batch_size', type = int ,  default=50) # Number of new samples per batch 
parser.add_argument('--n_init', type = int ,  default=10000) # Number of initial points

args, _ = parser.parse_known_args()

# ===========================================================================


# Params 
random_seed = args.seed # random seed 
soft_mkdir('results')
soft_mkdir(f'results/{args.name}')
soft_mkdir(f'results/simulation_{random_seed}')

print(f'>>> Running with {args.n_init} init samples and {args.bo_batch_size} batch size')


model_name = args.model
if '250k' in model_name:
        alphabet = '250k_alphabets.json'
elif 'zinc' in args.name:
    alphabet = 'zinc_alphabets.json'
else:
    alphabet = 'moses_alphabets.json'
print(f'Using alphabet : {alphabet}. Make sure it is coherent with args.model = {args.model}')


# Helper functions used to load and save objects

def save_object(obj, filename):

    """
    Function that saves an object to a file using pickle
    """

    result = pickle.dumps(obj)
    with gzip.GzipFile(filename, 'wb') as dest: dest.write(result)
    dest.close()


def load_object(filename):

    """
    Function that loads an object from a file using pickle
    """
    with gzip.GzipFile(filename, 'rb') as source: result = source.read()
    ret = pickle.loads(result)
    source.close()

    return ret


np.random.seed(random_seed)
start = time()

# We load the data
if args.obj != 'docking':
    X = np.loadtxt('../../data/latent_features_and_targets/latent_features.txt')
    y = -np.loadtxt(f'../../data/latent_features_and_targets/targets_{args.obj}.txt')
    X= X[:args.n_init,]
    y= y[:args.n_init]
else:
    X = np.loadtxt('../../data/latent_features_and_targets/latent_features_docking.txt')
    # We want to minimize docking scores => no need to take (-scores)
    y = -np.loadtxt(f'../../data/latent_features_and_targets/targets_{args.obj}.txt')
    PYTHONSH, VINA = set_path(args.server)
    
    with open('250k_docking_scores.pickle', 'rb') as f :
        docked = pickle.load(f)
    
    def dock_one(enum_tuple):
        """ Docks one smiles. Input = tuple from enumerate iterator"""
        identifier, smiles = enum_tuple
        if smiles in docked :
            return docked[smiles]
        else:
            return dock(smiles, identifier, PYTHONSH, VINA, parallel=False, exhaustiveness = 16)
    

y = y.reshape((-1, 1))
n = X.shape[ 0 ]
permutation = np.random.choice(n, n, replace = False)

X_train = X[ permutation, : ][ 0 : np.int(np.round(0.9 * n)), : ]
X_test = X[ permutation, : ][ np.int(np.round(0.9 * n)) :, : ]

y_train = y[ permutation ][ 0 : np.int(np.round(0.9 * n)) ]
y_test = y[ permutation ][ np.int(np.round(0.9 * n)) : ]

# Loading the model : 
        
# Loader for initial sample
loader = Loader(props=[],
                targets=[],
                csv_path = None,
                maps_path ='../map_files',
                alphabet_name = alphabet,
                vocab='selfies',
                num_workers = 0,
                test_only=True)

# Load model (on gpu if available)
device = 'cuda' if torch.cuda.is_available() else 'cpu' # the model device 
gp_device =  'cpu' #'cuda' if torch.cuda.is_available() else 'cpu' # gaussian process device 
model = model_from_json(model_name)
model.to(device)
model.eval()


iteration = 0

# ============ Iter loop ===============
while iteration < args.n_iters:

    # We fit the GP

    np.random.seed(iteration * random_seed)
    M = 500
    sgp = SparseGP(X_train, 0 * X_train, y_train, M)
    sgp.train_via_ADAM(X_train, 0 * X_train, y_train, X_test, X_test * 0,  \
        y_test, minibatch_size = 10 * M, max_iterations = args.epochs, learning_rate = 0.0005)

    pred, uncert = sgp.predict(X_test, 0 * X_test)
    error = np.sqrt(np.mean((pred - y_test)**2))
    testll = np.mean(sps.norm.logpdf(pred - y_test, scale = np.sqrt(uncert)))
    print('Test RMSE: ', error)
    print('Test ll: ', testll)

    pred, uncert = sgp.predict(X_train, 0 * X_train)
    error = np.sqrt(np.mean((pred - y_train)**2))
    trainll = np.mean(sps.norm.logpdf(pred - y_train, scale = np.sqrt(uncert)))
    print('Train RMSE: ', error)
    print('Train ll: ', trainll)

    # We pick the next 50 inputs

    next_inputs = sgp.batched_greedy_ei(args.bo_batch_size, np.min(X_train, 0), np.max(X_train, 0))
    
    # We decode the 50 smiles: 
    # Decode z into smiles
    with torch.no_grad():
        gen_seq = model.decode(torch.FloatTensor(next_inputs).to(device))
        smiles = model.probas_to_smiles(gen_seq)
        valid_smiles_final = []
        for s in smiles :
            s = decoder(s)
            m = Chem.MolFromSmiles(s)
            if m is None : 
                valid_smiles_final.append(None)
            else:
                Chem.Kekulize(m)
                s= Chem.MolToSmiles(m, kekuleSmiles = True)
                valid_smiles_final.append(s)


    new_features = next_inputs
    save_object(valid_smiles_final, f"results/simulation_{random_seed}/valid_smiles_{iteration}.dat")
    
    if args.obj == 'logp':

        logP_values = np.loadtxt('../../data/latent_features_and_targets/logP_values.txt')
        SA_scores = np.loadtxt('../../data/latent_features_and_targets/SA_scores.txt')
        cycle_scores = np.loadtxt('../../data/latent_features_and_targets/cycle_scores.txt')
    
        scores = []
        for i in range(len(valid_smiles_final)):
            if valid_smiles_final[ i ] is not None:
                m= MolFromSmiles(valid_smiles_final[ i ])
                
                current_log_P_value = logP(m)
                current_SA_score = -calculateScore(m)
                current_cycle_score = -cycle_score(m)
                
                # Normalize 
                current_SA_score_normalized = (current_SA_score - np.mean(SA_scores)) / np.std(SA_scores)
                current_log_P_value_normalized = (current_log_P_value - np.mean(logP_values)) / np.std(logP_values)
                current_cycle_score_normalized = (current_cycle_score - np.mean(cycle_scores)) / np.std(cycle_scores)
    
                score = (current_SA_score_normalized + current_log_P_value_normalized + current_cycle_score_normalized)
            else:
                score = -max(y)[ 0 ]

            scores.append(-score)
        
    elif args.obj == 'qed':
        
        qed_values = np.loadtxt('../../data/latent_features_and_targets/qed_values.txt')
        SA_scores = np.loadtxt('../../data/latent_features_and_targets/SA_scores.txt')
        cycle_scores = np.loadtxt('../../data/latent_features_and_targets/cycle_scores.txt')
    
        scores = []
        for i in range(len(valid_smiles_final)):
            if valid_smiles_final[ i ] is not None:
                m= MolFromSmiles(valid_smiles_final[ i ])
                
                current_qed_value = qed(m)
                current_SA_score = -calculateScore(m)
                current_cycle_score = -cycle_score(m)
                
                # Normalize 
                current_SA_score_normalized = (current_SA_score - np.mean(SA_scores)) / np.std(SA_scores)
                current_qed_value_normalized = (current_qed_value - np.mean(qed_values)) / np.std(qed_values)
                current_cycle_score_normalized = (current_cycle_score - np.mean(cycle_scores)) / np.std(cycle_scores)
    
                score = (current_SA_score_normalized + current_qed_value_normalized + current_cycle_score_normalized)
            else:
                score = -max(y)[ 0 ]

            scores.append(-score)
            
    elif args.obj == 'qsar':
        raise NotImplementedError
        
    elif args.obj == 'docking': # we want to minimize docking scores => no need to take (-score) as for other objectives 
        
        pool = Pool()
        scores = pool.map(dock_one, enumerate(valid_smiles_final))
        pool.close()
        
        raw_scores = np.array(scores)
        # normalize 
        targets_distrib = np.loadtxt(f'../../data/latent_features_and_targets/targets_docking.txt')
        scores = (raw_scores - np.mean(targets_distrib) ) / np.std(targets_distrib)
        
        # add to known scores : 
        for i in range(len(valid_smiles_final)):
            m=Chem.MolFromSmiles(valid_smiles_final[i])
            s= Chem.MolToSmiles(m, kekuleSmiles = True)
            if s not in docked : 
                docked[s] = raw_scores[i] # unnormalized docking scores 
                
        with open('250k_docking_scores.pickle', 'wb') as f :
            pickle.dump(docked, f)
        
        
        
    # Common to all objectives ; saving scores and smiles for this step 
    print(i)
    print(valid_smiles_final)
    print(scores)

    save_object(scores, f"results/{args.name}/simulation_{random_seed}/scores_{iteration}.dat")

    if len(new_features) > 0:
        X_train = np.concatenate([ X_train, new_features ], 0)
        y_train = np.concatenate([ y_train, np.array(scores)[ :, None ] ], 0)

    iteration += 1
    
    end = time()
    duration = end-start
    # write running time 
    print('Step time: ', duration)
    with open(f"results/{args.name}/simulation_{random_seed}/time.txt", 'w') as f :
        f.write(str(duration))
        
    print(iteration)
