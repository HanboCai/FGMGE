import numpy as np
import tensorflow as tf
import scipy.io.wavfile as wav
import os
import sys
import math
import copy
sys.path.append("DeepSpeech")
import random
from deap import base
from deap import creator
from deap import tools
from scipy.signal import butter, lfilter
from time import *


BETA0 = 1
GAMMA = 1
ALPHA = 1   #alpha's value will be changed


tf.load_op_library = lambda x: x
generation_tmp = os.path.exists
os.path.exists = lambda x: True
toolbox = base.Toolbox()

class Wrapper:
    def __init__(self, d):
        self.d = d
    def __getattr__(self, x):
        return self.d[x]

class HereBeDragons:
    d = {}
    FLAGS = Wrapper(d)
    def __getattr__(self, x):
        return self.do_define
    def do_define(self, k, v, *x):
        self.d[k] = v

tf.app.flags = HereBeDragons()
import DeepSpeech
os.path.exists = generation_tmp

# More monkey-patching, to stop the training coordinator setup
DeepSpeech.TrainingCoordinator.__init__ = lambda x: None
DeepSpeech.TrainingCoordinator.start = lambda x: None

from util.text import ctc_label_dense_to_sparse
from tf_logits import compute_mfcc, get_logits

# These are the tokens that we're allowed to use.
# The - token is special and corresponds to the epsilon
# value in CTC decoding, and can not occur in the phrase.
toks = " abcdefghijklmnopqrstuvwxyz'-"

###########################################################################

def db(audio):
    if len(audio.shape) > 1:
        maxx = np.max(np.abs(audio), axis=1)
        return 20 * np.log10(maxx) if np.any(maxx != 0) else np.array([0])
    maxx = np.max(np.abs(audio))
    return 20 * np.log10(maxx) if maxx != 0 else np.array([0])

def load_wav(input_wav_file):
    # Load the inputs that we're given
    fs, audio = wav.read(input_wav_file)
    assert fs == 16000
    print('source dB', db(audio))
    return audio

def save_wav(audio, output_wav_file):
    wav.write(output_wav_file, 16000, np.array(np.clip(np.round(audio), -2**15, 2**15-1), dtype=np.int16))
    print('output dB', db(audio))
    
def levenshteinDistance(s1, s2): 
    if len(s1) > len(s2):
        s1, s2 = s2, s1
        
    distances = range(len(s1) + 1) 
    for i2, c2 in enumerate(s2):
        distances_ = [i2+1]
        for i1, c1 in enumerate(s1):
            if c1 == c2:
                distances_.append(distances[i1])
            else:
                distances_.append(1 + min((distances[i1], distances[i1 + 1], distances_[-1])))
        distances = distances_
    return distances[-1]

def highpass_filter(data, cutoff=7000, fs=16000, order=10):
    b, a = butter(order, cutoff / (0.5 * fs), btype='high', analog=False)
    return lfilter(b, a, data)

def get_new_pop(elite_pop, elite_pop_scores, pop_size):
    scores_logits = np.exp(elite_pop_scores - elite_pop_scores.max()) 
    elite_pop_probs = scores_logits / scores_logits.sum()
    cand1 = elite_pop[np.random.choice(len(elite_pop), p=elite_pop_probs, size=pop_size)]
    cand2 = elite_pop[np.random.choice(len(elite_pop), p=elite_pop_probs, size=pop_size)]
    mask = np.random.rand(pop_size, elite_pop.shape[1]) < 0.5 
    next_pop = mask * cand1 + (1 - mask) * cand2
    return next_pop

def mutate_pop(pop, mutation_p, noise_stdev):
    noise = np.random.randn(*pop.shape) * noise_stdev
    noise = highpass_filter(noise)
    #mask = np.random.rand(pop.shape[0], elite_pop.shape[1]) < mutation_p
    mask = np.random.rand(pop.shape[0], pop.shape[1]) < mutation_p
    new_pop = pop + noise * mask
    mutant = toolbox.clone(new_pop)
    ind2, = tools.mutGaussian(mutant, mu=0.0001, sigma=0.2, indpb=0.2)
    return ind2

class Genetic():
    
    def __init__(self, input_wave_file, output_wave_file, target_phrase):
        self.pop_size = 40
        self.elite_size = 1
        self.mutation_p = 0.005
        #self.alpha1 = alpha1
        self.noise_stdev = 40
        self.noise_threshold = 1
        self.mu = 0.9
        self.alpha = 0.001
        self.max_iters = 3000
        self.num_points_estimate = 40
        self.delta_for_gradient = 100
        self.delta_for_perturbation = 1e3
        self.input_audio = load_wav(input_wave_file).astype(np.float32)
        self.pop = np.expand_dims(self.input_audio, axis=0)
        self.upper = max(self.input_audio)
        self.lower = min(self.input_audio)
        self.pop = np.tile(self.pop, (self.pop_size, 1))
        self.output_wave_file = output_wave_file
        self.target_phrase = target_phrase
        self.funcs = self.setup_graph(self.pop, np.array([toks.index(x) for x in target_phrase]))
        self.params = [BETA0, GAMMA, ALPHA]
        self.count = 0

    def setup_graph(self, input_audio_batch, target_phrase): 
        batch_size = input_audio_batch.shape[0]
        weird = (input_audio_batch.shape[1] - 1) // 320 
        logits_arg2 = np.tile(weird, batch_size)
        dense_arg1 = np.array(np.tile(target_phrase, (batch_size, 1)), dtype=np.int32)
        dense_arg2 = np.array(np.tile(target_phrase.shape[0], batch_size), dtype=np.int32)
        
        pass_in = np.clip(input_audio_batch, -2**15, 2**15-1)
        seq_len = np.tile(weird, batch_size).astype(np.int32)
        
        with tf.variable_scope('', reuse=tf.AUTO_REUSE):
            
            inputs = tf.placeholder(tf.float32, shape=pass_in.shape, name='a')
            len_batch = tf.placeholder(tf.float32, name='b')
            arg2_logits = tf.placeholder(tf.int32, shape=logits_arg2.shape, name='c')
            arg1_dense = tf.placeholder(tf.float32, shape=dense_arg1.shape, name='d')
            arg2_dense = tf.placeholder(tf.int32, shape=dense_arg2.shape, name='e')
            len_seq = tf.placeholder(tf.int32, shape=seq_len.shape, name='f')
            
            logits = get_logits(inputs, arg2_logits)
            target = ctc_label_dense_to_sparse(arg1_dense, arg2_dense, len_batch)
            ctcloss = tf.nn.ctc_loss(labels=tf.cast(target, tf.int32), inputs=logits, sequence_length=len_seq)
            decoded, _ = tf.nn.ctc_greedy_decoder(logits, arg2_logits, merge_repeated=True)
            
            sess = tf.Session()
            saver = tf.train.Saver(tf.global_variables())
            saver.restore(sess, "models/session_dump")
            
        func1 = lambda a, b, c, d, e, f: sess.run(ctcloss, 
            feed_dict={inputs: a, len_batch: b, arg2_logits: c, arg1_dense: d, arg2_dense: e, len_seq: f})
        func2 = lambda a, b, c, d, e, f: sess.run([ctcloss, decoded], 
            feed_dict={inputs: a, len_batch: b, arg2_logits: c, arg1_dense: d, arg2_dense: e, len_seq: f})
        return (func1, func2)

    def getctcloss(self, input_audio_batch, target_phrase, decode=False):
        batch_size = input_audio_batch.shape[0]
        weird = (input_audio_batch.shape[1] - 1) // 320 
        logits_arg2 = np.tile(weird, batch_size)
        dense_arg1 = np.array(np.tile(target_phrase, (batch_size, 1)), dtype=np.int32)
        dense_arg2 = np.array(np.tile(target_phrase.shape[0], batch_size), dtype=np.int32)
        
        pass_in = np.clip(input_audio_batch, -2**15, 2**15-1)
        seq_len = np.tile(weird, batch_size).astype(np.int32)

        if decode:
            return self.funcs[1](pass_in, batch_size, logits_arg2, dense_arg1, dense_arg2, seq_len)
        else:
            return self.funcs[0](pass_in, batch_size, logits_arg2, dense_arg1, dense_arg2, seq_len)
        
    def get_fitness_score(self, input_audio_batch, target_phrase, input_audio, classify=False):
        target_enc = np.array([toks.index(x) for x in target_phrase]) 
        if classify:
            ctcloss, decoded = self.getctcloss(input_audio_batch, target_enc, decode=True)
            all_text = "".join([toks[x] for x in decoded[0].values]) 
            index = len(all_text) // input_audio_batch.shape[0] 
            final_text = all_text[:index]
        else:
            ctcloss = self.getctcloss(input_audio_batch, target_enc)
        score = -ctcloss
        if classify:
            return (score, final_text) 
        return score, -ctcloss

    def move(self, _popFitnessScore, _nowPop, _bestPop):
        temp = copy.deepcopy(self.pop)
        temp = np.tile(_bestPop, (self.pop_size, 1))
        temp = mutate_pop(temp, self.mutation_p, self.noise_stdev)
        '''
                for indiv in range(0, temp.shape[0]):
            temp[indiv] = _bestPop
        '''
        self.count += 1

        mutataFlag = False
        for i in range(0, self.pop_size):
            for j in range(0,self.pop_size):
            #for j in range(0, i):
                    if _popFitnessScore[j] > _popFitnessScore[i]:
                        mutataFlag = True
                        r = np.linalg.norm(temp[i] - temp[j])
                        beta = self.params[0] * np.exp(-1 * self.params[1] * (r ** 2))
                        #temp[i]a += beta * (temp[j] - temp[i]) + self.params[2] * self.GetNewNestViaLevy(_nowPop,_bestPop, i)
                        #temp[i] += beta * (temp[j] - temp[i]) + self.GetNewNestViaLevy(_nowPop, _bestPop, i)
                        temp[i] += beta * (temp[j] - temp[i]) + 0.4 * np.random.rand(temp[i].shape[0])
                    else:
                        continue
        print("Current iteration number: ", self.count, " Whether the population is mutated in the current iteration: ", mutataFlag, "Alpha of this round:", self.params[2])
        return temp

    def alpha_new(self, _nowItr):
        return math.pow(0.97, 400 * _nowItr / 3000)

    

    def GetNewNestViaLevy(self, Xt, Xbest, _index):
        beta = 1.5
        sigma_u = (math.gamma(1 + beta) * math.sin(math.pi * beta / 2) / (
                math.gamma((1 + beta) / 2) * beta * (2 ** ((beta - 1) / 2)))) ** (1 / beta)
        sigma_v = 1
        for i in range(Xt.shape[0]):
            if i == _index:
                s = Xt[i, :]
                u = np.random.normal(0, sigma_u, 1)
                v = np.random.normal(0, sigma_v, 1)
                Ls = u / ((abs(v)) ** (1 / beta))
                stepsize = self.params[2] * Ls * (s - Xbest)
                s = s + stepsize * np.random.randn(1, len(s))
                Xt[i, :] = s
                Xt[i, :] = self.simplebounds(s)
            else:
                continue
        return Xt[_index]

    def simplebounds(self, s):
        for i in range(s.shape[0]):
            for j in range(s.shape[1]):
                if s[i][j] < self.lower:
                    s[i][j] = self.lower
                if s[i][j] > self.upper:
                    s[i][j] = self.upper
        return s

    def run(self, log=None):
        max_fitness_score = float('-inf')
        dist = float('inf')
        best_text = ''
        itr = 1
        prev_loss = None
        self.pop = mutate_pop(self.pop, self.mutation_p, self.noise_stdev)
        if log is not None:
            log.write('target phrase: ' + self.target_phrase + '\n')
            log.write('itr, corr, lev dist \n')
        
        while itr <= self.max_iters and best_text != self.target_phrase:
            pop_scores, ctc = self.get_fitness_score(self.pop, self.target_phrase, self.input_audio)
            elite_ind = np.argsort(pop_scores)[-self.elite_size:]
            elite_pop, elite_pop_scores, elite_ctc = self.pop[elite_ind], pop_scores[elite_ind], ctc[elite_ind]
            
            if prev_loss is not None and prev_loss != elite_ctc[-1]:
                self.mutation_p = self.mu * self.mutation_p + self.alpha / np.abs(prev_loss - elite_ctc[-1]) 

            if itr % 10 == 0:
                print('**************************** ITERATION {} ****************************'.format(itr))
                print('Current loss: {}'.format(-elite_ctc[-1]))
                save_wav(elite_pop[-1], self.output_wave_file)
                best_pop = np.tile(np.expand_dims(elite_pop[-1], axis=0), (40, 1))
                _, best_text = self.get_fitness_score(best_pop, self.target_phrase, self.input_audio, classify=True)
                dist = levenshteinDistance(best_text, self.target_phrase)
                corr = "{0:.4f}".format(np.corrcoef([self.input_audio, elite_pop[-1]])[0][1])
                print('Audio similarity to input: {}'.format(corr))
                print('Edit distance to target: {}'.format(dist))
                print('Currently decoded as: {}'.format(best_text))
                print(self.pop)
                print(elite_pop[-1])
                #print(popNum)
                if log is not None:
                    log.write(str(itr) + ", " + corr + ", " + str(dist) + "\n")

            if itr == 1:
                print('Current loss: {}'.format(-elite_ctc[-1]))
                prev_loss = elite_ctc[-1]

                    
            #if dist > 2:
                # next_pop = get_new_pop(elite_pop, elite_pop_scores, self.pop_size)

            fireflyPop = self.move(pop_scores, self.pop, elite_pop)
            # fireflypop = mutate_pop(fireflyPop, self.mutation_p, self.noise_stdev)#yth
            # print(4.1)
            self.params[2] = self.alpha_new(itr)
            # print(4.2)
            fireflyScores, fireflyCtc = self.get_fitness_score(fireflyPop, self.target_phrase, self.input_audio)
            # print(4.3)
            elite_ind = np.argsort(pop_scores)[-self.elite_size:]
            fireflyEliteIndex = np.argsort(fireflyScores)[-self.elite_size:]
            # print(4.4)
            '''
            if pop_scores[elite_ind] > fireflyScores[fireflyEliteIndex]:
                elite_pop, elite_pop_scores, elite_ctc = self.pop[elite_ind], pop_scores[elite_ind], ctc[elite_ind]
            else:
                elite_pop, elite_pop_scores, elite_ctc = fireflyPop[fireflyEliteIndex], fireflyScores[fireflyEliteIndex], fireflyCtc[fireflyEliteIndex]
                '''
            elite_pop, elite_pop_scores, elite_ctc = fireflyPop[fireflyEliteIndex], fireflyScores[
                fireflyEliteIndex], fireflyCtc[fireflyEliteIndex]
            # print(4.5)
            prev_loss = elite_ctc[-1]
            # print(4.6)
            self.pop = fireflyPop
            # print(4.7)
            if (prev_loss - elite_ctc[-1]) < 1:
                next_pop = get_new_pop(elite_pop, elite_pop_scores, self.pop_size)
                self.pop = mutate_pop(next_pop, self.mutation_p, self.noise_stdev)
                prev_loss = elite_ctc[-1]
            '''
            else:

                perturbed = np.tile(np.expand_dims(elite_pop[-1], axis=0), (self.num_points_estimate, 1))
                indices = np.random.choice(self.pop.shape[1], size=self.num_points_estimate, replace=False)

                perturbed[np.arange(self.num_points_estimate), indices] += self.delta_for_gradient
                perturbed_scores = self.get_fitness_score(perturbed, self.target_phrase, self.input_audio)[0]

                grad = (perturbed_scores - elite_ctc[-1]) / self.delta_for_gradient
                grad /= np.abs(grad).max()
                modified = elite_pop[-1].copy()
                modified[indices] += grad * self.delta_for_perturbation

                self.pop = np.tile(np.expand_dims(modified, axis=0), (self.pop_size, 1))
                self.delta_for_perturbation *= 0.995
             '''
                
            itr += 1

        return itr < self.max_iters
        
inp_wav_file = sys.argv[1]
target = sys.argv[2].lower()
out_wav_file = inp_wav_file[:-4] + '_adv.wav'
log_file = inp_wav_file[:-4] + '_log.txt'

print('target phrase:', target)
print('source file:', inp_wav_file)

g = Genetic(inp_wav_file, out_wav_file, target)
with open(log_file, 'w') as log:
    begin_time=time()
    success = g.run(log=log)
    end_time=time()
    run_time = end_time-begin_time
if success:
    print('Success! Wav file stored as', out_wav_file)
    print('Time is:',run_time)
else:
    print('Not totally a success! Consider running for more iterations. Intermediate output stored as', out_wav_file)
