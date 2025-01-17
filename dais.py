# -*- coding: utf-8 -*-
"""
Created on Thu Jun 22 15:45:58 2017

@author: Chin-Wei

Domain Adaptation with Importance Sampling
"""





from modules import LinearFlowLayer, IndexLayer, PermuteLayer
from modules import CoupledDenseLayer, stochastic_weight_norm
import theano
import theano.tensor as T
from theano.tensor.shared_randomstreams import RandomStreams
RSSV = T.shared_randomstreams.RandomStateSharedVariable
floatX = theano.config.floatX

import lasagne
from lasagne import nonlinearities
rectify = nonlinearities.rectify
softmax = nonlinearities.softmax
from lasagne.layers import get_output



from BHNs import MLPWeightNorm_BHN
from ops import load_mnist
from utils import log_normal, log_laplace, log_sum_exp
import numpy as np

import theano
import theano.tensor as T
import os
from lasagne.random import set_rng
#from theano.tensor.shared_randomstreams import RandomStreams


lrdefault = 1e-3    
    
class MCdropout_MLP(object):

    def __init__(self,n_hiddens,n_units):
        
        layer = lasagne.layers.InputLayer([None,784])
        
        self.n_hiddens = n_hiddens
        self.n_units = n_units
        self.weight_shapes = list()        
        self.weight_shapes.append((784,n_units))
        for i in range(1,n_hiddens):
            self.weight_shapes.append((n_units,n_units))
        self.weight_shapes.append((n_units,10))
        self.num_params = sum(ws[1] for ws in self.weight_shapes)
        
        
        for j,ws in enumerate(self.weight_shapes):
            layer = lasagne.layers.DenseLayer(
                layer,ws[1],
                nonlinearity=lasagne.nonlinearities.rectify
            )
            if j!=len(self.weight_shapes)-1:
                layer = lasagne.layers.dropout(layer)
        
        layer.nonlinearity = lasagne.nonlinearities.softmax
        self.input_var = T.matrix('input_var')
        self.target_var = T.matrix('target_var')
        self.learning_rate = T.scalar('leanring_rate')
        
        self.layer = layer
        self.y = lasagne.layers.get_output(layer,self.input_var)
        self.y_det = lasagne.layers.get_output(layer,self.input_var,
                                               deterministic=True)
        
        losses = lasagne.objectives.categorical_crossentropy(self.y,
                                                             self.target_var)
        self.loss = losses.mean()
        self.params = lasagne.layers.get_all_params(self.layer)
        self.updates = lasagne.updates.adam(self.loss,self.params,
                                            self.learning_rate)

        print '\tgetting train_func'
        self.train_func_ = theano.function([self.input_var,
                                            self.target_var,
                                            self.learning_rate],
                                           self.loss,
                                           updates=self.updates)
        
        print '\tgetting useful_funcs'
        self.predict_proba = theano.function([self.input_var],self.y)
        self.predict = theano.function([self.input_var],self.y_det.argmax(1))
        
    def train_func(self,x,y,n,lr=lrdefault,w=1.0):
        return self.train_func_(x,y,lr)

    def save(self,save_path,notes=[]):
        np.save(save_path, [p.get_value() for p in self.params]+notes)

    def load(self,save_path):
        values = np.load(save_path)
        notes = values[-1]
        values = values[:-1]

        if len(self.params) != len(values):
            raise ValueError("mismatch: got %d values to set %d parameters" %
                             (len(values), len(self.params)))

        for p, v in zip(self.params, values):
            if p.get_value().shape != v.shape:
                raise ValueError("mismatch: parameter has shape %r but value to "
                                 "set has shape %r" %
                                 (p.get_value().shape, v.shape))
            else:
                p.set_value(v)

        return notes


class MLPWeightNorm_BHN_dais(MLPWeightNorm_BHN):
   
    def _get_primary_net(self):
        t = np.cast['int32'](0)
        p_net = lasagne.layers.InputLayer([None,784])
        inputs = {p_net:self.input_var}
        self.hiddens = list() # for projection 
        for ws in self.weight_shapes:
            # using weightnorm reparameterization
            # only need ws[1] parameters (for rescaling of the weight matrix)
            num_param = ws[1]
            weight = self.weights[:,t:t+num_param].reshape((self.wd1,ws[1]))
            p_net = lasagne.layers.DenseLayer(p_net,ws[1])
            p_net = stochastic_weight_norm(p_net,weight)
            print p_net.output_shape
            t += num_param
            
            self.hiddens.append(p_net)
            
        p_net.nonlinearity = nonlinearities.softmax # replace the nonlinearity
                                                    # of the last layer
                                                    # with softmax for
                                                    # classification
        
        y = T.clip(get_output(p_net,inputs), 0.001, 0.999) # stability
        self.hs = get_output(self.hiddens,self.input_var)
        self.p_net = p_net
        self.y = y
        
    def _get_useful_funcs(self):
        super(MLPWeightNorm_BHN_dais, self)._get_useful_funcs()
        
        self.project = theano.function([self.input_var],self.hs)
        
        input2 = T.matrix('input2')
        h2 = get_output(self.hiddens,input2)
        y2 = get_output(self.p_net,input2)
        self.dais_ = theano.function([self.input_var,
                                      self.target_var,
                                      input2,
                                      self.weight,
                                      self.dataset_size],
                                     [self.loss,y2]+h2)
        
        imps_ = T.vector('imps_')
        logsoftmax_exp = theano.function([imps_],
                                         T.exp(imps_-log_sum_exp(imps_)))
        
        def dais_y(refx,refy,newx,n_iw,n=None):
            
            if n is None:
                n = refx.shape[0]
            imps = np.zeros(n_iw).astype('float32')
            ys = np.zeros((n_iw,newx.shape[0],
                           self.n_classes)).astype('float32')
            for i in range(n_iw):
                outs = self.dais_(refx,refy,newx,1.0,n)
                imps[i] = outs[0]
                ys[i] = outs[1]
            
            imps = logsoftmax_exp(imps)
            return (ys * imps[:,None,None]).sum(0)
        
        def dais_h(refx,refy,newx,n_iw,n=None):
            
            if n is None:
                n = refx.shape[0]
            imps = np.zeros(n_iw).astype('float32')
            hs = list()
            for i in range(n_iw):
                outs = self.dais_(refx,refy,newx,1.0,n)
                imps[i] = outs[0]
                hs.append(outs[2:])
            
            imps = logsoftmax_exp(imps)
            ind = np.random.multinomial(1,imps).argmax()
            return hs[ind]
        
        self.dais_y = dais_y
        self.dais_h = dais_h
        
            
    

def train_model(train_func,predict_func,X,Y,Xt,Yt,
                lr0=0.1,lrdecay=1,bs=20,epochs=50,anneal=0,name='0',
                e0=0,rec=0):
    
    print 'trainset X.shape:{}, Y.shape:{}'.format(X.shape,Y.shape)
    N = X.shape[0]    
    rec_name = name+'_recs'
    save_path = name + '.params'
    recs = list()
    
    t = 0
    for e in range(epochs):
        
        if e <= e0:
            continue
        
        if lrdecay:
            lr = lr0 * 10**(-e/float(epochs-1))
        else:
            lr = lr0         
        
        if anneal:
            w = min(1.0,0.001+e/(epochs/2.))
        else:
            w = 1.0         
            
        for i in range(N/bs):
            x = X[i*bs:(i+1)*bs]
            y = Y[i*bs:(i+1)*bs]
            
            loss = train_func(x,y,N,lr,w)
            
            if t%100==0:
                print 'epoch: {} {}, loss:{}'.format(e,t,loss)
                tr_acc = (predict_func(X)==Y.argmax(1)).mean()
                te_acc = (predict_func(Xt)==Yt.argmax(1)).mean()
                print '\ttrain acc: {}'.format(tr_acc)
                print '\ttest acc: {}'.format(te_acc)
            t+=1
        
        va_acc = evaluate_model(model.predict_proba,Xt,Yt,20)
        print '\n\nva acc at epochs {}: {}'.format(e,va_acc)    
        
        recs.append(va_acc)
        
        if va_acc > rec:
            print '.... save best model .... '
            model.save(save_path,[e])
            rec = va_acc
    
            with open(rec_name,'a') as rec_file:
                for r in recs:
                    rec_file.write(str(r)+'\n')
            
            recs = list()
            
        print '\n\n'



def evaluate_model(predict_proba,X,Y,n_mc=100,max_n=100):
    MCt = np.zeros((n_mc,X.shape[0],10))
    
    N = X.shape[0]
    num_batches = np.ceil(N / float(max_n)).astype(int)
    for i in range(n_mc):
        for j in range(num_batches):
            x = X[j*max_n:(j+1)*max_n]
            MCt[i,j*max_n:(j+1)*max_n] = predict_proba(x)
    
    Y_pred = MCt.mean(0).argmax(-1)
    Y_true = Y.argmax(-1)
    return np.equal(Y_pred,Y_true).mean()


    
if __name__ == '__main__':
    
    import argparse
    
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--perdatapoint',default=0,type=int)
    parser.add_argument('--lrdecay',default=1,type=int)      
    parser.add_argument('--lr0',default=0.001,type=float)  
    parser.add_argument('--coupling',default=12,type=int) 
    parser.add_argument('--lbda',default=1.0,type=float)  
    parser.add_argument('--size',default=50000,type=int)      
    parser.add_argument('--bs',default=20,type=int)  
    parser.add_argument('--epochs',default=5,type=int)
    parser.add_argument('--prior',default='log_normal',type=str)
    parser.add_argument('--model',default='BHN_MLPWN',type=str)
    parser.add_argument('--anneal',default=1,type=int)
    parser.add_argument('--n_hiddens',default=2,type=int)
    parser.add_argument('--n_units',default=1200,type=int)
    parser.add_argument('--totrain',default=0,type=int)
    parser.add_argument('--seed',default=427,type=int)
    parser.add_argument('--dais',default=1,type=int)
    
    args = parser.parse_args()
    print args
    
    
    set_rng(np.random.RandomState(args.seed))
    np.random.seed(args.seed+1000)

    
    if args.prior == 'log_normal':
        pr = 0
    if args.prior == 'log_laplace':
        pr = 1
    
    
    if args.model == 'BHN_MLPWN':
        md = 0
    if args.model == 'MCdropout_MLP':
        md = 1
    
    
    path = 'models'
    name = './{}/mnistWN_md{}nh{}nu{}c{}pr{}lbda{}lr0{}lrd{}an{}s{}seed{}'.format(
        path,
        md,
        args.n_hiddens,
        args.n_units,
        args.coupling,
        pr,
        args.lbda,
        args.lr0,
        args.lrdecay,
        args.anneal,
        args.size,
        args.seed
    )

    coupling = args.coupling
    perdatapoint = args.perdatapoint
    lrdecay = args.lrdecay
    lr0 = args.lr0
    lbda = np.cast['float32'](args.lbda)
    bs = args.bs
    epochs = args.epochs
    n_hiddens = args.n_hiddens
    n_units = args.n_units
    anneal = args.anneal
    if args.prior=='log_normal':
        prior = log_normal
    elif args.prior=='log_laplace':
        prior = log_laplace
    else:
        raise Exception('no prior named `{}`'.format(args.prior))
    size = max(10,min(50000,args.size))
    
    if os.path.isfile('/data/lisa/data/mnist.pkl.gz'):
        filename = '/data/lisa/data/mnist.pkl.gz'
    elif os.path.isfile(r'./data/mnist.pkl.gz'):
        filename = r'./data/mnist.pkl.gz'
    else:        
        print '\n\tdownloading mnist'
        import download_datasets.mnist
        filename = r'./data/mnist.pkl.gz'

    train_x, train_y, valid_x, valid_y, test_x, test_y = load_mnist(filename)
    
    if args.model == 'BHN_MLPWN':
        model = MLPWeightNorm_BHN_dais(lbda=lbda,
                                       perdatapoint=perdatapoint,
                                       srng = RandomStreams(seed=args.seed+2000),
                                       prior=prior,
                                       coupling=coupling,
                                       n_hiddens=n_hiddens,
                                       n_units=n_units)
    elif args.model == 'MCdropout_MLP':
        model = MCdropout_MLP(n_hiddens=n_hiddens,
                              n_units=n_units)
    else:
        raise Exception('no model named `{}`'.format(args.model))

    rec_name = name+'_recs'
    save_path = name + '.params.npy'
    if os.path.isfile(save_path):
        print 'load best model'
        e0 = model.load(save_path)
        recs = open(rec_name,'r').read().split('\n')[:e0]
        rec = max([float(r) for r in recs])
        
    else:
        e0 = 0
        rec = 0

    input('0')
    if args.totrain:
        print '\nstart training from epoch {}'.format(e0)
        train_model(model.train_func,model.predict,
                    train_x[:size],train_y[:size],
                    valid_x,valid_y,
                    lr0,lrdecay,bs,epochs,anneal,name,
                    e0,rec)
    else:
        print '\nno training'
    
    tr_acc = evaluate_model(model.predict_proba,
                            train_x[:size],train_y[:size])
    print 'train acc: {}'.format(tr_acc)
                   
    va_acc = evaluate_model(model.predict_proba,
                            valid_x,valid_y)
    print 'valid acc: {}'.format(va_acc)
    
    te_acc = evaluate_model(model.predict_proba,
                            test_x,test_y)
    print 'test acc: {}'.format(te_acc)


    if args.dais:
        
        import cPickle as pickle
        from tsne import bh_sne
        import matplotlib.pyplot as plt
        
        bs=10
        #ref = 0.005
        num = 10160
        num_ref = 256
        num_test = 1000
        X, Y = pickle.load(open('char74k_syndigs','r'))
        X = X.reshape(num,784)
        rng = np.random.RandomState(seed=args.seed)
        ind = rng.permutation(num)
        Xr, Yr = X[ind[:num_ref]], Y[ind[:num_ref]]
        Xt, Yt = X[ind[-num_test:]], Y[ind[-num_test:]]
        
        
        # classes / n_iw = 1, 10
        n_iw = 1
        n=np.inf
        Hs = np.zeros((num_test,1200)).astype('float32')
        for i in range(num_test/bs):
            x = Xt[i*bs:(i+1)*bs]
            y = Yt[i*bs:(i+1)*bs]
            Hs[i*bs:(i+1)*bs] = model.dais_h(Xr,Yr,x,n_iw,n)[0]
         
        
        Hs2 = bh_sne(Hs.astype('float64'))
        
        plt.figure() 
        plt.scatter(Hs2[:,0],Hs2[:,1],c=Yt.argmax(1))


        # domains / n_iw = 1, 10
        n_iw = 256
        n=np.inf
        
        Hs1 = np.zeros((num_test,1200)).astype('float32')
        for i in range(num_test/bs):
            x = Xt[i*bs:(i+1)*bs] 
            y = Yt[i*bs:(i+1)*bs]
            Hs1[i*bs:(i+1)*bs] = model.dais_h(Xr,Yr,x,n_iw,n)[0]
        
        
        Hs2 = np.zeros((num_test,1200)).astype('float32')
        for i in range(num_test/bs):
            x = test_x[i*bs:(i+1)*bs]
            y = test_y[i*bs:(i+1)*bs]
            Hs2[i*bs:(i+1)*bs] = model.project(x)[0]
            
        H = np.concatenate([Hs1,Hs2],0)
        H = bh_sne(H.astype('float64'),theta=0.5)
        
        
        plt.figure() 
        plt.scatter(H[:num_test,0],H[:num_test,1],c='red',alpha=0.4)
        plt.scatter(H[num_test:,0],H[num_test:,1],c='blue',alpha=0.4)
        plt.xlim(-30,30)
        plt.ylim(-30,30)
        plt.savefig('h1iw256.jpg')

        # acc ## 0.753
        acc_mc = evaluate_model(model.predict_proba,Xt,Yt,n_mc=256,max_n=100)

        n_iw = 256
        n=np.inf
        y_iw = np.zeros(num_test).astype('float32')
        for i in range(num_test/bs):
            x = Xt[i*bs:(i+1)*bs]
            y = Yt[i*bs:(i+1)*bs]
            y_iw[i*bs:(i+1)*bs] = model.dais_y(Xr,Yr,x,n_iw,n).argmax(1)
        acc_iw = (y_iw == Yt.argmax(1)).mean()
        print acc_mc, acc_iw
        

    
        evaluate_model(model.predict_proba,test_x[:1000],test_y[:1000],n_mc=100,max_n=100)

    