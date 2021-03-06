from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Categorical

from torch.utils.data import Dataset

import gym
import numpy as np
import torch
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm

import time
import timeit

from chess_env import ChessEnv

# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

device = torch.device("cuda")

torch.manual_seed(0)


class Episode:
    def __init__(self):
        self.observations = []
        self.actions = []
        self.advantages = []
        self.rewards = []
        self.rewards_to_go = []
        self.values = []
        self.log_probabilities = []
        self.masks = []


class History(Dataset):
    def __init__(self):
        self.episodes = []
        self.observations = []
        self.actions = []
        self.advantages = []
        self.rewards = []
        self.rewards_to_go = []
        self.log_probabilities = []
        self.masks = []

    def free_memory(self):
        del self.episodes[:]
        del self.observations[:]
        del self.actions[:]
        del self.advantages[:]
        del self.rewards[:]
        del self.rewards_to_go[:]
        del self.log_probabilities[:]
        del self.masks[:]

    def build_dataset(self):
        for episode in self.episodes:
            self.observations += episode.observations
            self.actions += episode.actions
            self.advantages += episode.advantages
            self.rewards += episode.rewards
            self.rewards_to_go += episode.rewards_to_go
            self.log_probabilities += episode.log_probabilities
            self.masks += episode.masks

       

        self.advantages = normalize_list(self.advantages)

    def __len__(self):
        return len(self.observations)

    def __getitem__(self, idx):
        return (
            self.observations[idx],
            self.actions[idx],
            self.advantages[idx],
            self.log_probabilities[idx],
            self.rewards_to_go[idx],
            self.masks[idx],
        )

class ResBlock(nn.Module):
    def __init__(self, inplanes=256, planes=256, stride=1, downsample=None):
        super(ResBlock, self).__init__()
        self.conv1 = nn.Conv2d(inplanes, planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)

    def forward(self, x):
        residual = x
        out = self.conv1(x)
        out = F.relu(self.bn1(out))
        out = self.conv2(out)
        out = self.bn2(out)
        out += residual
        out = F.relu(out)
        return out

class OutBlockactor(nn.Module):
    def __init__(self):
        super(OutBlockactor, self).__init__()     
        self.conv1 = nn.Conv2d(256, 128, kernel_size=1) # policy head
        self.bn1 = nn.BatchNorm2d(128)
        self.fc = nn.Linear(8*8*128, 4272)
    
    def forward(self,s):
        p = F.relu(self.bn1(self.conv1(s))) # policy head
        p = p.view(-1, 8*8*128)
        p = self.fc(p)
        return p


class OutBlockcritic(nn.Module):
    def __init__(self):
        super(OutBlockcritic, self).__init__()
        self.conv = nn.Conv2d(256, 1, kernel_size=1) # value head
        self.bn = nn.BatchNorm2d(1)
        self.fc1 = nn.Linear(8*8, 64)
        self.fc2 = nn.Linear(64, 1)
   
    def forward(self,s):
        v = F.relu(self.bn(self.conv(s))) # value head
        v = v.view(-1, 8*8)  # batch_size X channel X height X width
        v = F.relu(self.fc1(v))
        v = torch.tanh(self.fc2(v))
        return v


class ActorNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1=nn.Conv2d(in_channels=21, out_channels=256, kernel_size=3,padding=1)
        self.bn1 = nn.BatchNorm2d(256)
        self.relu1=nn.ReLU(inplace=True)
        for block in range(21):
            setattr(self, "res_%i" % block,ResBlock())
        self.outblockactor = OutBlockactor()


    def forward(self, x):
        y = self.conv1(x)
        y = self.bn1(y)
        y = self.relu1(y)
        for block in range(21):
            y = getattr(self, "res_%i" % block)(y)
        y = self.outblockactor(y)
        return y

class CriticNN(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1=nn.Conv2d(in_channels=21, out_channels=256, kernel_size=3,padding=1)
        self.bn1 = nn.BatchNorm2d(256)
        self.relu1=nn.ReLU(inplace=True)
        for block in range(21):
            setattr(self, "res_%i" % block,ResBlock())
        self.outblockcritic = OutBlockcritic()
        self.saved_log_probs = []
        self.rewards = []

    def forward(self, x):
        y = self.conv1(x)
        y = self.bn1(y)
        y = self.relu1(y)
        for block in range(21):
            y = getattr(self, "res_%i" % block)(y)
        y = self.outblockcritic(y)
        return y.squeeze(1)



def normalize_list(array):
    array = np.array(array)
    array = (array - np.mean(array)) / (np.std(array) + 1e-5)
    return array.tolist()

def get_action(state):

    policy_model.eval()
    value_model.eval()

    if not state is torch.Tensor:
        state = torch.from_numpy(state).float().to(device)

    if state.shape[0] != 1:
        state = state.unsqueeze(0) # Create batch dimension

    logits = policy_model(state)

    legal_actions = torch.tensor(env.legal_actions()).to(device)
    mask = torch.zeros(4272).to(device)
    mask.index_fill_(0,legal_actions, 1)
    logits[0][mask == 0] = -float("Inf")


    m = Categorical(logits=logits)

    action = m.sample()

    log_probability = m.log_prob(action)

    value = value_model(state)

    return action.item(), log_probability.item(), value.item(), mask


def cumulative_sum(vector, discount):
    out = np.zeros_like(vector)
    n = vector.shape[0]
    for i in reversed(range(n)):
        out[i] =  vector[i] + discount * (out[i+1] if i+1 < n else 0)
    return out.tolist()

def end_episode(last_value):
    # Calculate trajectory rewards to go
    # Calculate trajectory GAE

    # REWARDS TO GO


    rewards = np.array(episode.rewards + [last_value])
    values = np.array(episode.values + [last_value])

    episode.rewards_to_go = cumulative_sum(rewards, gamma)[:-1]

    # GAE
    
    deltas = rewards[:-1] + gamma * values[1:] - values[:-1]

    episode.advantages = cumulative_sum(deltas, gamma * gae_lambda)



def train_network(data_loader):
    policy_epoch_losses = []
    value_epoch_losses = []

    policy_model.train()
    value_model.train()

    global train_ite

    c1 = 0.01

    for i in range(n_epoch):

        policy_losses = []
        value_losses = []

        for observations, actions, advantages, log_probabilities, rewards_to_go, masks in data_loader:
            observations = observations.float().to(device)
            actions = actions.long().to(device)
            advantages = advantages.float().to(device)
            old_log_probabilities = log_probabilities.float().to(device)
            rewards_to_go = rewards_to_go.float().to(device)
            masks = masks.to(device)

            
            logits = policy_model(observations)

            
            for i in range(masks.shape[0]):
                logits[i][masks[i] == 0] = -float("Inf")

            m = Categorical(logits=logits)

            entropy = m.entropy()

            new_log_probabilities = m.log_prob(actions)

            values = value_model(observations)


            probability_ratios = torch.exp(new_log_probabilities - old_log_probabilities)
            clipped_probabiliy_ratios = torch.clamp(
                probability_ratios, 1 - clip, 1 + clip
            )

            surrogate_1 = probability_ratios * advantages
            surrogate_2 = clipped_probabiliy_ratios * advantages

            Actor_loss = -torch.min(surrogate_1, surrogate_2).mean() - c1 * entropy.mean()

            Critic_loss = F.mse_loss(values, rewards_to_go)

            policy_optimizer.zero_grad()

            value_optimizer.zero_grad()

            loss = Actor_loss + Critic_loss
            loss.backward()

            value_optimizer.step()

            policy_optimizer.step()

            
            

            

            policy_losses.append(Actor_loss.item())

            
            value_losses.append(Critic_loss.item())

        policy_epoch_losses.append(np.mean(policy_losses))
        value_epoch_losses.append(np.mean(value_losses))

    train_ite +=1
    
    for name, weight in policy_model.named_parameters():
        writer.add_histogram(name,weight, train_ite)
        writer.add_histogram(f'{name}.grad',weight.grad, train_ite)

    return policy_epoch_losses, value_epoch_losses



# env_name = "CartPole-v1"
# env_name = "LunarLander-v2"
env_name = "Chess"


learning_rate = 5e-4
state_scale = 1.0
reward_scale = 1.0
clip = 0.2

# env = gym.make(env_name)
env = ChessEnv()

observation = env.reset()

# n_actions = env.action_space.n
# feature_dim = observation.size

value_model = CriticNN().to(device)
value_optimizer = optim.Adam(value_model.parameters(), lr=learning_rate)

policy_model = ActorNN().to(device)
policy_optimizer = optim.Adam(policy_model.parameters(), lr=learning_rate)

n_epoch = 8

max_episodes = 8 #antes tenía 8
max_timesteps = 60

batch_size = 16 #antes tenía 16

max_iterations = 1000

gamma = 0.99
gae_lambda = 0.95

history = History()

epoch_ite = 0
episode_ite = 0
time_steps_ite = 0
train_ite = 0

#Load model if it exists
model_path = "./model"
if (Path(model_path) / (env_name + "policy.pth")).exists():
    policy_model.load_state_dict(torch.load(Path(model_path) / (env_name + "policy.pth")))
    value_model.load_state_dict(torch.load(Path(model_path) / (env_name + "value.pth")))
    print("Loaded model!")

running_reward = -1000

timestr = time.strftime("%d%m%Y-%H%M%S-")

log_dir = "./runs/" + timestr + env_name + "-BS" + str(batch_size) + "-E" + \
        str(max_episodes) + "-MT" + str(max_timesteps) + "-NE" + str(n_epoch) + \
        "-LR" + str(learning_rate) + "-G" + str(gamma) + "-L" + str(gae_lambda)

writer = SummaryWriter(log_dir=log_dir)

for ite in tqdm(range(max_iterations), ascii=True):

    if ite % 5 == 0:
        torch.save(policy_model.state_dict(),Path(model_path) / (env_name + "policy.pth"))
        torch.save(value_model.state_dict(),Path(model_path) / (env_name + "value.pth"))

    print("\nSimulating")

    observation = env.reset()
    ep_reward = 0
    for episode_i in range(max_episodes):
        
        episode = Episode()

        for timestep in range(max_timesteps):
            # Loop through time_steps

            # env.render()

            action, log_probability, value, mask = get_action(observation / state_scale)

            new_observation, reward, done = env.step(action)

            ep_reward +=reward

            episode.observations.append(observation / state_scale)
            episode.actions.append(action)
            episode.rewards.append(reward / reward_scale)
            episode.values.append(value)
            episode.log_probabilities.append(log_probability)
            episode.masks.append(mask)

            time_steps_ite += 1
            writer.add_scalar(
                "Movement Probabilities",
                np.exp(log_probability),
                time_steps_ite,
            )
 

            observation = new_observation

            if done:
                end_episode(last_value=0)
                episode_ite += 1

                writer.add_scalar(
                    "Average Episode Reward",
                    ep_reward,
                    episode_ite,
                )
                

                running_reward = 0.05 * ep_reward + (1 - 0.05) * running_reward

                #Reset episode rewards and enviroment because one episode finished
                ep_reward = 0
                observation = env.reset(np.random.randint(0,100)%10==0)
                break

            if timestep == max_timesteps - 1:
                # Episode didn't finish so we have to append value to RTGs and advantages
                _, _, value, _ = get_action(observation / state_scale)
                end_episode(last_value=value)

        


        # At this point we have collected a trajectory of T time_steps
        # This is not a full episode.

        history.episodes.append(episode)
    
    # Here we have collected N trajectories.
    history.build_dataset()

    data_loader = DataLoader(history, batch_size=batch_size, shuffle=True,drop_last=True)

    print("Training")

    policy_loss, value_loss = train_network(data_loader)


    for p_l, v_l in zip(policy_loss, value_loss):
        epoch_ite += 1
        writer.add_scalar("Policy Loss", p_l, epoch_ite)
        writer.add_scalar("Value Loss", v_l, epoch_ite)

    history.free_memory()

    # print("\n", running_reward)

    writer.add_scalar("Running Reward", running_reward, epoch_ite)




    if (running_reward > 20):
        print("\nSolved!")
        break