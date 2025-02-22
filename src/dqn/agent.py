import random
import logging
import numpy as np
from typing import Tuple, Any

import torch
import torch.nn.functional as F
import torch.optim as optim

from src.dqn.network import QNetwork
from src.dqn.memory import PrioritizedMemory, ReplayMemory
from src.base import Agent

_logger = logging.getLogger(__name__)

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class Agent(Agent):
    """Interacts with and learns from the environment."""

    def __init__(self, state_size: int, action_size: int, seed: int = 1993,
                 nb_hidden: tuple = (64, 64), learning_rate: float = 5e-4,
                 memory_size: int = int(1e5), prioritized_memory: bool = False,
                 batch_size: int = 64, gamma: float = 0.99, tau: float = 1e-3,
                 small_eps: float = 1e-5, update_every: int = 4,
                 epsilon_enabled: bool = True, epsilon_start: float = 1.0,
                 epsilon_end: float = 0.01, epsilon_decay: float = 0.995,
                 **kwargs):
        """
        Initialize a Deep Q-Network agent.
        
        Parameters:
        - state_size: dimension of each state.
        - action_size: dimension of each action.
        - seed: random seed.
        - nb_hidden: number of hidden units in the network.
        - learning_rate: learning rate for the optimizer.
        - memory_size: size of the memory.
        - prioritized_memory: if True, use prioritized memory.
        - batch_size: size of the batch.
        - gamma: discount factor.
        - tau: interpolation parameter for target network.
        - small_eps: small value used in the priority update.
        - update_every: number of steps before updating the target network.
        - epsilon_enabled: if True, use epsilon-greedy action selection.
        - epsilon_start: starting value of epsilon, for epsilon-greedy action selection.
        - epsilon_end: minimum value of epsilon.
        - epsilon_decay: decay rate for epsilon.
        """
        self.state_size = state_size
        self.action_size = action_size
        self.seed = random.seed(seed)
        self.learning_rate = learning_rate
        self.memory_size = memory_size
        self.prioritized_memory = prioritized_memory
        self.batch_size = batch_size
        self.gamma = gamma
        self.tau = tau
        self.small_eps = small_eps
        self.update_every = update_every
        self.epsilon_enabled = epsilon_enabled
        self.epsilon_start = epsilon_start
        self.epsilon_end = epsilon_end
        self.epsilon_decay = epsilon_decay

        # Initialize epsilon
        if self.epsilon_enabled:
            self.epsilon = self.epsilon_start
        else:
            self.epsilon = 0.0

        self.losses = []

        # Q-Network
        self.qnet_local = QNetwork(self.state_size, self.action_size, layers=nb_hidden, seed=seed).to(device)
        self.qnet_target = QNetwork(self.state_size, self.action_size, layers=nb_hidden, seed=seed).to(device)       
        self.optimizer = optim.Adam(self.qnet_local.parameters(), lr=self.learning_rate)

        # Define memory
        if self.prioritized_memory:
            self.memory = PrioritizedMemory(self.memory_size, self.batch_size)
        else:
            self.memory = ReplayMemory(self.memory_size, self.batch_size)
            
        # Initialize time step (for updating every UPDATE_EVERY steps)
        self.t_step = 0

        _logger.info("DQN Agent initialized.")

    def logs(self):
        """
        Get the logs of the agent.

        Returns:
        - A string with the log message.
        """
        # Compute the avg loss of the last 100 episodes
        if len(self.losses) > 0:
            avg_loss = int(torch.mean(torch.stack(self.losses[-100:])))
        else:
            avg_loss = float("inf")

        return f"Epsilon: {self.epsilon:.2f}\tAvg. MSE Loss: {avg_loss:.2f}"
        
    def step(self, state: np.ndarray, action: int, reward: int,
             next_state: np.ndarray, done: bool, episode: int, **kwargs):
        """
        Save experience in replay memory, and use experiences from memory to learn.
        
        Args:
        - state: current state
        - action: action taken
        - reward: reward received
        - next_state: next state
        - done: if the episode is over
        - episode: current episode (only used for prioritized memory).
        """
        _logger.debug({"message": "Registering step", "state": state,
                       "action": action, "reward": reward, "next_state": next_state,
                       "done": done, "episode": episode})

        # Preprocessing states
        state = self.prep_state(state)
        next_state = self.prep_state(next_state)

        # Save experience in replay memory
        self.memory.add(state, action, reward, next_state, done)
        
        # Learn every UPDATE_EVERY time steps.
        self.t_step = (self.t_step + 1) % self.update_every
        if self.t_step == 0:
            # If enough samples are available in memory, get random subset and learn
            if len(self.memory) > self.batch_size:
                if self.prioritized_memory:
                    experiences = self.memory.sample(self.get_beta(episode))
                else:
                    experiences = self.memory.sample()
                    
                self.learn(experiences)             

    def decay_eps(self):
        """
        Decay epsilon-greedy used for action selection.
        """
        self.epsilon = max(self.epsilon_end, self.epsilon_decay * self.epsilon)

    def act(self, state: np.ndarray) -> int:
        """Returns actions for given state as per current policy.
        
        Args:
        - state: current state.

        Returns:
        - action: an action.
        """
        _logger.debug(f"Acting on state {state}")

        # Preprocessing state
        state = self.prep_state(state)

        state = torch.from_numpy(state).float().unsqueeze(0).to(device)

        self.qnet_local.eval()
        with torch.no_grad():
            action_values = self.qnet_local(state)
        self.qnet_local.train()

        # Epsilon-greedy action selection
        if self.epsilon_enabled and random.random() < self.epsilon:
            action = random.randrange(self.action_size)
        else:
            action = np.argmax(action_values.cpu().data.numpy()).astype(int)
            
        if self.epsilon_enabled:
            self.decay_eps()

        _logger.debug(f"Returning action: {action}")

        return action

    def learn(self, experiences: Tuple[Any]):
        """
        Update value parameters using given batch of experience tuples.
        
        Args:
        - experiences: tuple of (s, a, r, s', done) tuples
        """
        _logger.debug("Starting learning.")

        if self.prioritized_memory:
            states, actions, rewards, next_states, dones, index, sampling_weights = experiences
        else:
            states, actions, rewards, next_states, dones = experiences

        # Get max predicted Q values (for next states) from target model
        Q_targets_next = self.qnet_target(next_states).detach().max(1)[0].unsqueeze(1)
        # Compute Q targets for current states 
        Q_targets = rewards + (self.gamma * Q_targets_next * (1 - dones))

        # Get expected Q values from local model
        Q_expected = self.qnet_local(states).gather(1, actions)

        # Compute loss
        if self.prioritized_memory:
            loss = self.mse_loss_prioritized(Q_expected, Q_targets, index, sampling_weights)
        else:
            loss = F.mse_loss(Q_expected, Q_targets)

        self.losses.append(loss)

        # Minimize the loss
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        # Update target network
        self.soft_update()

        _logger.debug("Finished learning.")                     

    def soft_update(self):
        """Soft update model parameters.
        θ_target = τ*θ_local + (1 - τ)*θ_target
        """
        for target_param, local_param in zip(self.qnet_target.parameters(), self.qnet_local.parameters()):
            target_param.data.copy_(self.tau * local_param.data + (1.0 - self.tau) * target_param.data)

    def save_model(self, path: str):
        """
        Save the model to the given path.

        Args:
        - path: path to save the model
        """
        _logger.debug(f"Saving model to path: {path}")

        torch.save(self.qnet_local.state_dict(), path)

    def load_model(self, path: str):
        """
        Load the model from the given path.

        Args:
        - path: path to load the model
        """
        _logger.debug(f"Loading model from path: {path}")
        
        self.qnet_local.load_state_dict(torch.load(path))
    
    def get_beta(self, i: int, beta_start: float = 0.4, beta_end: int = 1, beta_growth: float = 1.05):
        """
        Returns the beta value for prioritized experience replay.

        Args:
        - i: current iteration
        - beta_start: beta value at iteration 0
        - beta_end: beta value at iteration beta_end
        - beta_growth: beta value grows by beta_growth each iteration

        Returns:
        - beta: beta value for the current iteration
        """
        if not self.prioritized_memory:
            raise TypeError("This agent is not using prioritized memory.")
            
        beta = min(beta_start * (beta_growth ** i), beta_end)
        return beta

    def mse_loss_prioritized(self, Q_expected: torch.Tensor, Q_targets: torch.Tensor,
                             index: np.ndarray, sampling_weights: torch.Tensor):
        """
        Compute the loss for prioritized experience replay.

        Args:
        - Q_expected: expected Q values
        - Q_targets: target Q values
        - index: indices of the experiences
        - sampling_weights: weights of the experiences

        Returns:
        - loss: mse loss
        """
        losses = F.mse_loss(Q_expected, Q_targets, reduce=False).squeeze(1) * sampling_weights
        self.memory.update_priority(index, losses + self.small_eps)
        return losses.mean()
