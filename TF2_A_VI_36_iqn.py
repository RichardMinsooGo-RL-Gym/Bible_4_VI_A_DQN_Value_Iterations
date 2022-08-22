import tensorflow as tf
import gym
import numpy as np

from tensorflow.keras import optimizers, losses
from tensorflow.keras import Model
from collections import deque

import collections
import random


class Embedding(Model):
    def __init__(self, embedding_dim):
        super(Embedding, self).__init__()
        self.layer = tf.keras.layers.Dense(embedding_dim, activation='relu')
        self.embedding_dim = embedding_dim

    def call(self, batch_size, num_quantile, tau_min, tau_max):
        sample = tf.random.uniform(
            [batch_size * num_quantile, 1],
            minval=tau_min, maxval=tau_max, dtype=tf.float32)
        sample_tile = tf.tile(sample, [1, self.embedding_dim])
        embedding = tf.cos(
            tf.cast(tf.range(0, self.embedding_dim, 1), tf.float32) * np.pi * sample_tile)
        embedding_out = self.layer(embedding)
        return embedding_out, sample

class IQN(Model):
    def __init__(self):
        super(IQN, self).__init__()
        
        self.num_action = 2
        self.embedding_dim = 64

        self.embedding_out = Embedding(self.embedding_dim)

        self.layer1 = tf.keras.layers.Dense(64, activation='relu')
        self.layer2 = tf.keras.layers.Dense(64, activation='relu')

        self.h_fc1 = tf.keras.layers.Dense(64, activation='relu')
        self.value = tf.keras.layers.Dense(self.num_action)

    def call(self, state, num_quantile, tau_min, tau_max):
        layer1 = self.layer1(state)
        h_flat = self.layer2(layer1)
        h_flat_tile = tf.tile(h_flat, [num_quantile, 1])
        
        embedding_out, sample = self.embedding_out(
            state.shape[0], num_quantile, tau_min, tau_max)
        
        h_flat_embedding = tf.multiply(h_flat_tile, embedding_out)
        
        h_fc1 = self.h_fc1(h_flat_embedding)
        logits = self.value(h_fc1)
        logits_reshape = tf.reshape(logits, [num_quantile, state.shape[0], self.num_action])
        
        Q_action = tf.reduce_mean(logits_reshape, axis=0)

        return logits_reshape, Q_action, sample

class Agent:
    def __init__(self):
        self.lr = 0.00025
        self.gamma = 0.99

        self.batch_size = 64
        self.state_size = 4
        self.action_size = 2

        self.get_action_num_quantile = 32
        self.get_action_tau_min = 0.0
        self.get_action_tau_max = 0.25

        self.train_num_quantile = 8
        self.train_tau_min = 0.0
        self.train_tau_max = 1.0

        self.train_num_quantile = 8
        
        self.iqn_model = IQN()
        self.iqn_target = IQN()
        self.opt = optimizers.Adam(lr=self.lr, epsilon=(1e-2 / self.batch_size))

        self.memory = collections.deque(maxlen=int(2000))

    def update_target(self):
        self.iqn_target.set_weights(self.iqn_model.get_weights())
    
    def get_action(self, state, epsilon):
        state = tf.convert_to_tensor([state], dtype=tf.float32)
        _, q_value, _ = self.iqn_model(
                        state, self.get_action_num_quantile,
                        self.get_action_tau_min, self.get_action_tau_max)
        q_value = q_value[0]
        if np.random.rand() <= epsilon:
            action = np.random.choice(self.action_size)
        else:
            action = np.argmax(q_value) 
        return action, q_value

    def append_sample(self, state, action, reward, next_state, done):
        self.memory.append([state, action, reward, next_state, done])

    def update(self):
        mini_batch = random.sample(self.memory, self.batch_size)

        states      = np.stack([i[0] for i in mini_batch])
        actions     = np.stack([i[1] for i in mini_batch])
        rewards     = np.stack([i[2] for i in mini_batch])
        next_states = np.stack([i[3] for i in mini_batch])
        dones       = np.stack([i[4] for i in mini_batch])

        _, Q_batch, _ = self.iqn_model(
            tf.convert_to_tensor(np.stack(next_states), dtype=tf.float32),
            self.train_num_quantile, self.train_tau_min,
            self.train_tau_max)
        theta_batch, _, _ = self.iqn_target(
            tf.convert_to_tensor(np.stack(next_states), dtype=tf.float32),
            self.train_num_quantile, self.train_tau_min,
            self.train_tau_max)
        Q_batch, theta_batch = np.array(Q_batch), np.array(theta_batch)

        theta_target = []
        for i in range(len(mini_batch)):
            theta_target.append([])
            for j in range(self.train_num_quantile):
                target_value = rewards[i] + self.gamma * (1-dones[i]) * theta_batch[j, i, np.argmax(Q_batch[i])]
                theta_target[i].append(target_value)

        action_binary = np.zeros([self.train_num_quantile, len(mini_batch), self.action_size])
        for i in range(len(actions)):
            action_binary[:, i, actions[i]] = 1

        iqn_variable = self.iqn_model.trainable_variables
        with tf.GradientTape() as tape:
            theta_target = tf.convert_to_tensor(theta_target, dtype=tf.float32)
            action_binary_loss = tf.convert_to_tensor(action_binary, dtype=tf.float32)
            logits, _, sample = self.iqn_model(
                        tf.convert_to_tensor(np.stack(states), dtype=tf.float32),
                        self.train_num_quantile, self.train_tau_min,
                        self.train_tau_max)
            theta_pred = tf.reduce_sum(tf.multiply(logits, action_binary_loss), axis=2)

            theta_target_tile = tf.tile(tf.expand_dims(theta_target, axis=0), [self.train_num_quantile, 1, 1])
            theta_pred_tile = tf.tile(tf.expand_dims(theta_pred, axis=2), [1, 1, self.train_num_quantile])

            error_loss = theta_target_tile - theta_pred_tile

            Huber_loss = tf.compat.v1.losses.huber_loss(theta_target_tile, theta_pred_tile, reduction = tf.losses.Reduction.NONE)

            tau = tf.reshape(sample, [self.train_num_quantile, -1, 1])
            tau = tf.tile(tau, [1, 1, self.train_num_quantile])
            inv_tau = 1.0 - tau

            Loss = tf.where(tf.less(error_loss, 0.0), inv_tau * Huber_loss, tau * Huber_loss)
            Loss = tf.reduce_mean(tf.reduce_sum(tf.reduce_mean(Loss, axis=-1), axis=0))

        grads = tape.gradient(Loss, iqn_variable)
        self.opt.apply_gradients(zip(grads, iqn_variable))
        

    def run(self):

        env = gym.make('CartPole-v0')
        episode = 0
        step = 0

        for episode in range(500):
            state = env.reset()
            done = False
            episode += 1
            epsilon = 1 / (episode * 0.1 + 1)
            score = 0
            while not done:
                step += 1
                action, q_value = self.get_action(state, epsilon)
                next_state, reward, done, info = env.step(action)
                self.append_sample(state, action, reward, next_state, done)
                score += reward

                state = next_state

                if step > 1000:
                    self.update()
                    if step % 20 == 0:
                        self.update_target()
            print(episode, score)

if __name__ == '__main__':
    agent = Agent()
    agent.run()
