import os
import torch as T
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from agents.replay_buffer import ReplayBuffer
import numpy as np


class Agent:
    def __init__(self, input_dims, alpha=0.001, beta=0.002, env=None, gamma=0.99, n_actions=2, warmup=1000, update_actor_iter = 2,
    max_size=1_000_000, tau=0.005, fc1=400, fc2=300, batch_size=300, noise=0.1, device= "cuda:3", chkpt_dir="") -> None:
        self.gamma = gamma
        self.tau = tau
        self.memory = ReplayBuffer(max_size, input_dims, n_actions)
        self.batch_size =batch_size
        self.n_actions = n_actions
        self.noise = noise
        self.max_action = env.action_space.high[0]
        self.min_action = env.action_space.low[0]
        self.learn_step_cntr = 0 # Delayed actor learning
        self.time_step = 0
        self.warmup = warmup
        self.update_actor_iter = update_actor_iter

        self.actor = ActorNetwork(state_dims=input_dims, n_actions=n_actions, 
                            fc1_dims=fc1, fc2_dims=fc2, name="actor", chkpt_dir=chkpt_dir)
        self.critic_1 = CriticNetwork(state_dims=input_dims, n_actions=n_actions, 
                                        fc1_dims=fc1, fc2_dims=fc2, name="critic_1", chkpt_dir=chkpt_dir)
        self.critic_2 = CriticNetwork(state_dims=input_dims, n_actions=n_actions, 
                                        fc1_dims=fc1, fc2_dims=fc2, name="critic_2", chkpt_dir=chkpt_dir)


        self.target_actor = ActorNetwork(state_dims=input_dims, n_actions=n_actions, 
                                        fc1_dims=fc1, fc2_dims=fc2, name="target_actor", chkpt_dir=chkpt_dir)
        self.target_critic_1 = CriticNetwork(state_dims=input_dims, n_actions=n_actions, 
                                        fc1_dims=fc1, fc2_dims=fc2, name="target_critic_1", chkpt_dir=chkpt_dir)
        self.target_critic_2 = CriticNetwork(state_dims=input_dims, n_actions=n_actions, 
                                        fc1_dims=fc1, fc2_dims=fc2, name="target_critic_2", chkpt_dir=chkpt_dir)

        self.device = T.device(device if T.cuda.is_available() else 'cpu')

        self.actor.to(self.device)
        self.target_actor.to(self.device)
        self.critic_1.to(self.device)
        self.target_critic_1.to(self.device)
        self.critic_2.to(self.device)
        self.target_critic_2.to(self.device)

        self.actor_optim = optim.Adam(self.actor.parameters(), lr=alpha)
        self.critic_1_optim = optim.Adam(self.critic_1.parameters(), lr=beta)
        self.critic_2_optim = optim.Adam(self.critic_2.parameters(), lr=beta)

        self.update_network_parameters(tau=1)

    def choose_action(self, observation, evaluate=False):
        if self.time_step < self.warmup and not evaluate:
            mu = T.tensor(np.random.normal(scale=self.noise, size=(self.n_actions)))
        else:
            self.actor.eval()
            
            state = T.tensor([observation], dtype=T.float32).to(self.device)
            actions = self.actor(state) * self.max_action
            mu = actions[0]

        if evaluate:
            return mu.cpu().detach().numpy()

        mu_prime = mu + np.random.normal(scale=self.noise)
        
        mu_prime = T.clip(mu_prime, min=self.min_action, max=self.max_action)

        self.time_step += 1
        self.actor.train()
        
        return mu_prime.cpu().detach().numpy()
    
    def remember(self, state, action, reward, new_state, done):
        self.memory.store_transition(state, action, reward, new_state, done)


    def learn(self):
        if self.memory.mem_cntr < self.batch_size:
            return None, None, None

        state, new_state, action, reward, done = self.memory.sample_buffer(self.batch_size)

        states = T.tensor(state, dtype=T.float32).to(self.device)
        states_ = T.tensor(new_state, dtype=T.float32).to(self.device)
        actions = T.tensor(action, dtype=T.float32).to(self.device)
        rewards = T.tensor(reward, dtype=T.float32).to(self.device)
        dones = T.tensor(done, dtype=T.int).to(self.device)

        self.target_actor.eval()
        self.target_critic_1.eval()
        self.target_critic_2.eval()
        self.critic_1.eval()
        self.critic_2.eval()
        with T.autograd.set_detect_anomaly(True):
            with T.no_grad():
                target_actions = self.target_actor(states_)
                target_actions = target_actions + T.clip(T.tensor(np.random.normal(scale=0.2)), -0.5, 0.5)
                target_actions = T.clip(target_actions, min=self.min_action, max=self.max_action)

                q1_ = self.target_critic_1(states_, target_actions)
                q2_ = self.target_critic_2(states_, target_actions)

                q_join = T.cat((q1_, q2_), axis=1)
                # print("q1_", q1_,"q2_", q2_ , "q_join", q_join)
                critic_value_ = T.min(q_join, axis=1)[0].view(-1)

                # print("critic_value_", critic_value_, "cat", T.cat((q1_, q2_)))

            target = rewards + self.gamma*critic_value_*(1-dones)

            q1 = self.critic_1(states, actions).squeeze(1)
            q2 = self.critic_2(states, actions).squeeze(1)

            self.critic_1.train()
            self.critic_2.train()

            self.critic_1_optim.zero_grad()
            critic_loss_1 = F.mse_loss(target, q1)
            critic_loss_1.backward(retain_graph=True)
            
            self.critic_2_optim.zero_grad()
            critic_loss_2 = F.mse_loss(target, q2)
            critic_loss_2.backward()

            self.critic_1_optim.step()
            self.critic_2_optim.step()

        self.learn_step_cntr += 1

        if self.learn_step_cntr % self.update_actor_iter !=0:
            return None, critic_loss_1.cpu().detach().numpy(),  critic_loss_2.cpu().detach().numpy()

        self.critic_1.eval()
        self.actor.train()
        self.actor_optim.zero_grad()
        new_policy_actions = self.actor(states)
        actor_loss = -self.critic_1(states, new_policy_actions)
        actor_loss = T.mean(actor_loss)

        actor_loss.backward()
        self.actor_optim.step()

        self.update_network_parameters()

        return actor_loss.cpu().detach().numpy(), critic_loss_1.cpu().detach().numpy(),  critic_loss_2.cpu().detach().numpy()

    def update_network_parameters(self, tau=None):
        if tau is None:
            tau = self.tau 


        targets = self.target_actor.state_dict()
        
        for key in self.actor.state_dict():
            targets[key] = self.actor.state_dict()[key] * tau + self.target_actor.state_dict()[key]*(1-tau)

        self.target_actor.load_state_dict(targets)

        targets = self.target_critic_1.state_dict()
        
        for key in self.critic_1.state_dict():
            targets[key] = self.critic_1.state_dict()[key] * tau + self.target_critic_1.state_dict()[key]*(1-tau)

        self.target_critic_1.load_state_dict(targets)    

        targets = self.target_critic_2.state_dict()
        
        for key in self.critic_2.state_dict():
            targets[key] = self.critic_2.state_dict()[key] * tau + self.target_critic_2.state_dict()[key]*(1-tau)

        self.target_critic_2.load_state_dict(targets)

    def save_models(self):
        print("..... saving models ....")

        T.save(self.actor.state_dict(), self.actor.checkpoint_file)
        T.save(self.critic_1.state_dict(), self.critic_1.checkpoint_file)
        T.save(self.critic_2.state_dict(), self.critic_2.checkpoint_file)
        T.save(self.target_actor.state_dict(), self.target_actor.checkpoint_file)
        T.save(self.target_critic_1.state_dict(), self.target_critic_1.checkpoint_file)
        T.save(self.target_critic_2.state_dict(), self.target_critic_2.checkpoint_file)

    def load_models(self):
        print("..... loading models ....")

        self.actor.load_state_dict(T.load(self.actor.checkpoint_file, map_location=self.device))
        self.critic_1.load_state_dict(T.load(self.critic_1.checkpoint_file, map_location=self.device))
        self.critic_2.load_state_dict(T.load(self.critic_2.checkpoint_file, map_location=self.device))
        self.target_actor.load_state_dict(T.load(self.target_actor.checkpoint_file, map_location=self.device))
        self.target_critic_1.load_state_dict(T.load(self.target_critic_1.checkpoint_file, map_location=self.device))
        self.target_critic_2.load_state_dict(T.load(self.target_critic_2.checkpoint_file, map_location=self.device))
    

class CriticNetwork(nn.Module):
    def __init__(self, state_dims, n_actions, fc1_dims=512, fc2_dims=512, name='critic', chkpt_dir="") -> None:
        super(CriticNetwork, self).__init__()
        self.fc1_dims = fc1_dims
        self.fc2_dims = fc2_dims
        self.n_actions = n_actions[0]
        self.state_dims = state_dims[0] 

        self.model_name = name
        self.checkpoint_dir = chkpt_dir
        self.checkpoint_file = os.path.join(self.checkpoint_dir, self.model_name + "_td3.pt")

        self.fc1 = nn.Linear(self.state_dims+ self.n_actions, self.fc1_dims)
        self.fc2 = nn.Linear(self.fc1_dims, self.fc2_dims)
        self.q = nn.Linear(self.fc2_dims, 1)

        print(self)

    def forward(self, state, action):
        action_value = F.relu(self.fc1(T.concat([state, action], axis=1)))
        action_value = F.relu(self.fc2(action_value))
        
        q = self.q(action_value)

        return q

class ActorNetwork(nn.Module):
    def __init__(self, state_dims, n_actions, fc1_dims=512, fc2_dims=512, name='actor', chkpt_dir="") -> None:
        super(ActorNetwork, self).__init__()
        self.fc1_dims = fc1_dims
        self.fc2_dims = fc2_dims
        self.n_actions = n_actions
        self.state_dims = state_dims 

        self.model_name = name
        self.checkpoint_dir = chkpt_dir
        self.checkpoint_file = os.path.join(self.checkpoint_dir, self.model_name + "_td3.pt")

        self.fc1 = nn.Linear(*self.state_dims, self.fc1_dims)
        self.fc2 = nn.Linear(self.fc1_dims, self.fc2_dims)
        self.mu = nn.Linear(self.fc2_dims, *self.n_actions)
        
        print(self)

    def forward(self, state):

        x = F.relu(self.fc1(state))
        x = F.relu(self.fc2(x))

        mu =  T.tanh(self.mu(x))

        return mu