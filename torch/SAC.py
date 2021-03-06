import argparse
from collections import namedtuple
from itertools import count
import pickle
from utils import run_test
import json
import os, random
import numpy as np
import mytorcs
import gym
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Normal
from tensorboardX import SummaryWriter
from torch.autograd import Variable
'''
Implementation of soft actor critic, dual Q network version 
Original paper: https://arxiv.org/abs/1801.01290
Not the author's implementation !
'''

device = 'cuda' if torch.cuda.is_available() else 'cpu'
parser = argparse.ArgumentParser()

parser.add_argument('--mode', default='train', type=str) # mode = 'train' or 'test'
parser.add_argument("--env_name", default="MyTorcs-v0")  # OpenAI gym environment name
parser.add_argument('--tau',  default=0.005, type=float) # target smoothing coefficient
parser.add_argument('--target_update_interval', default=1, type=int)
parser.add_argument('--gradient_steps', default=10, type=int)

parser.add_argument('--learning_rate', default=3e-4, type=float)
parser.add_argument('--gamma', default=0.9998, type=int) # discount gamma
parser.add_argument('--capacity', default=100000, type=int) # replay buffer size
parser.add_argument('--iteration', default=10000, type=int) #  num of  games
parser.add_argument('--batch_size', default=128, type=int) # mini batch size
parser.add_argument('--seed', default=1362, type=int)

# optional parameters
parser.add_argument('--num_hidden_layers', default=2, type=int)
parser.add_argument('--num_hidden_units_per_layer', default=256, type=int)
parser.add_argument('--sample_frequency', default=256, type=int)
parser.add_argument('--activation', default='Relu', type=str)
parser.add_argument('--render', default=False, type=bool) # show UI or not
parser.add_argument('--log_interval', default=50, type=int) #
parser.add_argument('--load', default=False, type=bool) # load model
parser.add_argument('--render_interval', default=100, type=int) # after render_interval, the env.render() will work
args = parser.parse_args()


env = gym.make(args.env_name)

# Set seeds
env.seed(args.seed)
torch.manual_seed(args.seed)
np.random.seed(args.seed)

state_dim = env.observation_space.shape[0]
action_dim = env.action_space.shape[0]
min_Val = torch.tensor(1e-7).float().to(device)

class Replay_buffer():
    def __init__(self, capacity):
        self.capacity = capacity
        self.state_pool = torch.zeros(self.capacity, state_dim).float().to(device)
        self.action_pool = torch.zeros(self.capacity, action_dim).float().to(device)
        self.reward_pool = torch.zeros(self.capacity, 1).float().to(device)
        self.next_state_pool = torch.zeros(self.capacity, state_dim).float().to(device)
        self.done_pool = torch.zeros(self.capacity, 1).float().to(device)
        self.num_transition = 0

    def push(self, s, a, r, s_, d):
        index = self.num_transition % self.capacity
        s = torch.tensor(s).float().to(device)
        a = torch.tensor(a).float().to(device)
        r = torch.tensor(r).float().to(device)
        s_ = torch.tensor(s_).float().to(device)
        d = torch.tensor(d).float().to(device)
        for pool, ele in zip([self.state_pool, self.action_pool, self.reward_pool, self.next_state_pool, self.done_pool],
                           [s, a, r, s_, d]):
            pool[index] = ele
        self.num_transition += 1

    def sample(self, batch_size):
        index = np.random.choice(range(self.capacity), batch_size, replace=False)
        bn_s, bn_a, bn_r, bn_s_, bn_d = self.state_pool[index], self.action_pool[index], self.reward_pool[index],\
                                        self.next_state_pool[index], self.done_pool[index]

        return bn_s, bn_a, bn_r, bn_s_, bn_d

class Actor(nn.Module):
    def __init__(self, state_dim, action_dim=action_dim, min_log_std=-10, max_log_std=2):
        super(Actor, self).__init__()
        self.fc1 = nn.Linear(state_dim, 400)
        self.fc2 = nn.Linear(400, 300)
        self.mu_head = nn.Linear(300, action_dim)
        self.log_std_head = nn.Linear(300, action_dim)

        self.min_log_std = min_log_std
        self.max_log_std = max_log_std

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        mu = self.mu_head(x)
        log_std_head = F.relu(self.log_std_head(x))
        log_std_head = torch.clamp(log_std_head, self.min_log_std, self.max_log_std)
        return mu, log_std_head


class Critic(nn.Module):
    def __init__(self, state_dim):
        super(Critic, self).__init__()
        self.fc1 = nn.Linear(state_dim, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, 1)

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x


class Q(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Q, self).__init__()
        self.fc1 = nn.Linear(state_dim + action_dim, 256)
        self.fc2 = nn.Linear(256, 256)
        self.fc3 = nn.Linear(256, 1)

    def forward(self, s, a):
        s = s.reshape(-1, state_dim)
        a = a.reshape(-1, action_dim)
        x = torch.cat((s, a), -1) # combination s and a
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x


class SAC():
    def __init__(self):
        super(SAC, self).__init__()

        self.policy_net = Actor(state_dim).to(device)
        self.value_net = Critic(state_dim).to(device)
        self.Target_value_net = Critic(state_dim).to(device)
        self.Q_net1 = Q(state_dim, action_dim).to(device)
        self.Q_net2 = Q(state_dim, action_dim).to(device)

        self.policy_optimizer = optim.Adam(self.policy_net.parameters(), lr=args.learning_rate)
        self.value_optimizer = optim.Adam(self.value_net.parameters(), lr=args.learning_rate)
        self.Q1_optimizer = optim.Adam(self.Q_net1.parameters(), lr=args.learning_rate)
        self.Q2_optimizer = optim.Adam(self.Q_net2.parameters(), lr=args.learning_rate)

        self.replay_buffer = Replay_buffer(args.capacity)
        self.num_transition = 0 # pointer of replay buffer
        self.num_training = 0
        self.writer = SummaryWriter('./exp-SAC_dual_Q_network')

        self.value_criterion = nn.MSELoss()
        self.Q1_criterion = nn.MSELoss()
        self.Q2_criterion = nn.MSELoss()

        for target_param, param in zip(self.Target_value_net.parameters(), self.value_net.parameters()):
            target_param.data.copy_(param.data)

        os.makedirs('./SAC_model/', exist_ok=True)

    def act(self, state):
        state = torch.FloatTensor(state).to(device)
        mu, log_sigma = self.policy_net(state)
        return torch.tanh(mu).detach().cpu().numpy()
        
    def select_action(self, state):
        state = torch.FloatTensor(state).to(device)
        mu, log_sigma = self.policy_net(state)
        sigma = torch.exp(log_sigma)
        dist = Normal(mu, sigma)
        z = dist.sample()
        action = torch.tanh(z).detach().cpu().numpy()
        return action # return a scalar, float32

    def perturb_action(self, s, epsilon = 0.01, method='fgsm', relative=False):
        s = torch.FloatTensor(s.reshape(1, -1)).to(device)
        state= Variable(s, requires_grad=True)
        delta_s = None

        if method == "random":
            rand = (np.random.randint(0, 2, state.shape)*2 - 1).astype(np.float32)
            if relative:
                delta_s = state.mul(torch.tensor(rand)) * epsilon
            else:
                delta_s = torch.tensor(rand * epsilon)
            state = (state + delta_s).clamp(-1, 1)
        elif method == "fgsm":
            Q1 = self.Q_net1(state, self.policy_net(state)[0])
            Q1.backward()
            g1 = state.grad
            state = Variable(state, requires_grad=True)
            Q2 = self.Q_net1(state, self.policy_net(state)[0].detach())
            Q2.backward()
            g2 = state.grad
            g = g1 - g2
            if relative:
                delta_s = state.mul(g.sign()) * epsilon
            else:
                delta_s = g.sign() * epsilon 
            state = (state + delta_s).clamp(-1, 1)
        elif method == "i-fgsm":
            for i in range(10):
                state= Variable(state, requires_grad=True)
                Q1 = self.critic_1(state, self.policy_net(state))
                Q1.backward()
                g1 = state.grad
                state = Variable(state, requires_grad=True)
                Q2 = self.critic_1(state, self.policy_net(state).detach())
                Q2.backward()
                g2 = state.grad
                g = g1 - g2
                if relative:
                    delta_s = state.mul(g.sign()) * (epsilon/10)
                else:
                    delta_s = g.sign() * (epsilon/10)
                state = (state + delta_s).clamp(-1, 1)


        return self.policy_net(state)[0].cpu().data.numpy().flatten()
    def evaluate(self, state):
        batch_mu, batch_log_sigma = self.policy_net(state)
        batch_sigma = torch.exp(batch_log_sigma)
        dist = Normal(batch_mu, batch_sigma)
        noise = Normal(0, 1)

        z = noise.sample()
        action = torch.tanh(batch_mu + batch_sigma*z.to(device))
    
        log_prob = dist.log_prob(batch_mu + batch_sigma * z.to(device)).sum(dim=1, keepdim=True) - (torch.log(1 - action.pow(2) + min_Val)).sum(dim=1, keepdim=True)
        return action, log_prob, z, batch_mu, batch_log_sigma

    def update(self):
        if self.num_training % 500 == 0:
            print("Training ... \t{} times ".format(self.num_training))

        for _ in range(args.gradient_steps):
            bn_s, bn_a, bn_r, bn_s_, bn_d = self.replay_buffer.sample(args.batch_size)

            target_value = self.Target_value_net(bn_s_)
            next_q_value = bn_r + (1 - bn_d) * args.gamma * target_value

            excepted_value = self.value_net(bn_s)
            excepted_Q1 = self.Q_net1(bn_s, bn_a)
            excepted_Q2 = self.Q_net2(bn_s, bn_a)
            sample_action, log_prob, z, batch_mu, batch_log_sigma = self.evaluate(bn_s)
            excepted_new_Q = torch.min(self.Q_net1(bn_s, sample_action), self.Q_net2(bn_s, sample_action))
            next_value = excepted_new_Q - log_prob

            # !!!Note that the actions are sampled according to the current policy,
            # instead of replay buffer. (From original paper)
            V_loss = self.value_criterion(excepted_value, next_value.detach()).mean()  # J_V

            # Dual Q net
            Q1_loss = self.Q1_criterion(excepted_Q1, next_q_value.detach()).mean() # J_Q
            Q2_loss = self.Q2_criterion(excepted_Q2, next_q_value.detach()).mean()

            pi_loss = (log_prob - excepted_new_Q).mean() # according to original paper

            self.writer.add_scalar('Loss/V_loss', V_loss, global_step=self.num_training)
            self.writer.add_scalar('Loss/Q1_loss', Q1_loss, global_step=self.num_training)
            self.writer.add_scalar('Loss/Q2_loss', Q2_loss, global_step=self.num_training)
            self.writer.add_scalar('Loss/policy_loss', pi_loss, global_step=self.num_training)

            # mini batch gradient descent
            self.value_optimizer.zero_grad()
            V_loss.backward(retain_graph=True)
            nn.utils.clip_grad_norm_(self.value_net.parameters(), 0.5)
            self.value_optimizer.step()

            self.Q1_optimizer.zero_grad()
            Q1_loss.backward(retain_graph = True)
            nn.utils.clip_grad_norm_(self.Q_net1.parameters(), 0.5)
            self.Q1_optimizer.step()

            self.Q2_optimizer.zero_grad()
            Q2_loss.backward(retain_graph = True)
            nn.utils.clip_grad_norm_(self.Q_net2.parameters(), 0.5)
            self.Q2_optimizer.step()

            self.policy_optimizer.zero_grad()
            pi_loss.backward(retain_graph = True)
            nn.utils.clip_grad_norm_(self.policy_net.parameters(), 0.5)
            self.policy_optimizer.step()

            # update target v net update
            for target_param, param in zip(self.Target_value_net.parameters(), self.value_net.parameters()):
                target_param.data.copy_(target_param * (1 - args.tau) + param * args.tau)

            self.num_training += 1

    def save(self):
        torch.save(self.policy_net.state_dict(), './SAC_model/policy_net.pth')
        torch.save(self.value_net.state_dict(), './SAC_model/value_net.pth')
        torch.save(self.Q_net1.state_dict(), './SAC_model/Q_net1.pth')
        torch.save(self.Q_net2.state_dict(), './SAC_model/Q_net2.pth')
        print("====================================")
        print("Model has been saved...")
        print("====================================")

    def load(self):
        self.policy_net.load_state_dict(torch.load('./SAC_model/policy_net.pth'))
        self.value_net.load_state_dict(torch.load( './SAC_model/value_net.pth'))
        self.Q_net1.load_state_dict(torch.load('./SAC_model/Q_net1.pth'))
        self.Q_net2.load_state_dict(torch.load('./SAC_model/Q_net2.pth'))
        print("====================================")
        print("model has been loaded...")
        print("====================================")

def main():
    agent = SAC()
    # if args.load: agent.load()
    print("====================================")
    print("Collection Experience...")
    print("====================================")

    ep_r = 0
    max_r = 0
    perturb = False
    if args.mode == "test":
        agent.load()
        r, t = run_test(agent, env, True)
        print("{} {}".format(r, t))
    elif args.mode == "perturb":
        res = dict()
        methods = ["fgsm", "random"]
        for m in methods:
            res[m] = run_perturb(agent, m, step=0.005, step_cnt=20, relative=False)
        with open("results/SAC", "w") as f:
            json.dump(res, f)
    else:
        while True:
            state = env.reset()
            for t in range(8000):
                action = env.action_space.sample()
                next_state, reward, done, info = env.step(action)
                agent.replay_buffer.push(state, action, reward, next_state, done)
                state = next_state
                if done:
                    break
            if agent.replay_buffer.num_transition >= args.capacity:
                break

        for i in range(args.iteration):
            state = env.reset()
            for t in range(8000):
                action = agent.select_action(state)
                next_state, reward, done, info = env.step((action))
                ep_r += reward
                if args.render and i >= args.render_interval : env.set_test()
                agent.replay_buffer.push(state, action, reward, next_state, done)
                state = next_state
                if done or t == 7999:
                    break

            agent.update()
            if i % 5 == 0:
                ep_r, t = run_test(agent, env)
                if ep_r > max_r and t > 800:
                    agent.save()
                    max_r = ep_r

            agent.writer.add_scalar('ep_r', ep_r, global_step=i)
            ep_r = 0


if __name__ == '__main__':
    main()