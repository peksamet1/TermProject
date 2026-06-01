from mesa import Agent
from core.data_loader import DataLoader

class ProsumerAgent(Agent):
    """
    Autonomous prosumer unit with personal energy generation and consumption profiles.
    Evaluates its net energy to determine its market role (Buyer, Seller, or Idle).
    """
    def __init__(self, unique_id: int, model, battery_capacity: float = 10.0):
        super().__init__(unique_id, model)
        self.data_loader = DataLoader(unique_id)
        
        # Physical Parameters
        self.battery_capacity = battery_capacity  # in kWh
        self.battery_soc = battery_capacity * 0.5 # Initial State of Charge: 50%
        
        # Current State
        self.current_generation = 0.0
        self.current_consumption = 0.0
        self.net_energy = 0.0
        
        # Market Status: "SURPLUS" (Seller), "DEFICIT" (Buyer), "IDLE"
        self.status = "IDLE" 

    def step(self):
        """
        Invoked by the Mesa scheduler at each simulation tick.
        Updates internal state and evaluates market strategy.
        """
        # 1. Fetch data from profiles
        current_step = self.model.schedule.steps
        self.current_generation = self.data_loader.get_current_generation(current_step)
        self.current_consumption = self.data_loader.get_current_consumption(current_step)
        
        # 2. Calculate net energy (Generation - Consumption)
        self.net_energy = self.current_generation - self.current_consumption
        
        # 3. Determine market participation strategy
        self.determine_market_status()

    def determine_market_status(self):
        """
        Determines the agent's role based on energy surplus or deficit.
        """
        if self.net_energy > 0:
            self.status = "SURPLUS"
            # TODO: Integrate ZeroMQ to broadcast a Sell Offer
        elif self.net_energy < 0:
            self.status = "DEFICIT"
            # TODO: Integrate ZeroMQ to broadcast a Buy Bid
        else:
            self.status = "IDLE"
            
        print(f"[Step {self.model.schedule.steps:02d}] Agent {self.unique_id:02d} | "
              f"Net Energy: {self.net_energy:>5.2f} kW | Status: {self.status}")