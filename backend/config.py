from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
STATIONS_FILE = ROOT_DIR / "Rattaparklad.csv"
GRAPH_FILE = ROOT_DIR / "cache" / "tartu_bike.graphml"
