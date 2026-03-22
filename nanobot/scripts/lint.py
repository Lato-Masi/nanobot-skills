import sys
import subprocess

def main():
    try:
        subprocess.run([f"{sys.executable}", "-m", "pyright"], check=True)
    except FileNotFoundError:
        print("pyright not found. Please install it with `pip install pyright`")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

if __name__ == "__main__":
    main()
