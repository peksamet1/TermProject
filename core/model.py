from mesa import Model
from mesa.time import RandomActivation
from core.agent import ProsumerAgent

class MicrogridModel(Model):
    """
    Decentralized microgrid environment managing agent activations.
    """
    def __init__(self, num_agents: int):
        super().__init__()
        self.num_agents = num_agents
        
        # RandomActivation mimics real-world asynchronous behavior well in local tests
        self.schedule = RandomActivation(self)
        self.running = True
        
        # Instantiate and register agents
        for i in range(self.num_agents):
            agent = ProsumerAgent(unique_id=i, model=self)
            self.schedule.add(agent)

    def step(self):
        """
        Advances the simulation by one time step.
        """
        self.schedule.step()
        # TODO: Implement macro-grid fallback checks here