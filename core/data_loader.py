import random

class DataLoader:
    """
    Mock data loader to simulate synthetic energy profiles.
    In future iterations, this will parse actual CSV/Pandas datasets.
    """
    def __init__(self, agent_id: int):
        self.agent_id = agent_id

    def get_current_generation(self, step: int) -> float:
        """Simulates solar PV generation in kW."""
        return round(random.uniform(0.0, 5.0), 2)

    def get_current_consumption(self, step: int) -> float:
        """Simulates household load profile in kW."""
        return round(random.uniform(0.5, 3.5), 2)