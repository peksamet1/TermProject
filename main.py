from core.model import MicrogridModel

def main():
    print("=== Starting P2P Microgrid Simulation ===")
    
    # Configuration
    NUM_AGENTS = 5
    SIMULATION_STEPS = 3
    
    # Initialize the model
    model = MicrogridModel(num_agents=NUM_AGENTS)
    
    # Run the simulation loop
    for step in range(SIMULATION_STEPS):
        print(f"\n--- Simulation Tick: {step} ---")
        model.step()
        
    print("\n=== Simulation Completed ===")

if __name__ == "__main__":
    main()