# Installation Guide

This guide provides detailed instructions for setting up the application for both development and runtime execution.

## 1. System Requirements

Before you begin, ensure your system meets the following requirements:

- **Python:** Version 3.11 or higher is required. You can download the latest version of Python from [python.org](https://python.org/downloads/).
- **Node.js:** Version 20 or higher is required for the WhatsApp bridge. You can download Node.js from [nodejs.org](https://nodejs.org/).
- **Git:** Required for cloning the repository and managing source code. You can download Git from [git-scm.com](https://git-scm.com/).
- **Docker:** (Optional) For running the application in a containerized environment. Visit [docker.com](https://www.docker.com/products/docker-desktop/) for installation instructions.

## 2. Development Setup

Follow these steps to set up the application for development:

### 2.1. Clone the Repository

First, clone the repository to your local machine using Git:

```bash
git clone <repository-url>
cd <repository-directory>
```

### 2.2. Create a Virtual Environment

It is highly recommended to use a virtual environment to manage project dependencies. Create and activate a virtual environment as follows:

```bash
python3 -m venv venv
source venv/bin/activate
```

### 2.3. Install Python Dependencies

The project uses `uv` for package management. Install the required Python dependencies, including development tools, by running:

```bash
pip install "uv>=0.1.0"
uv pip install -e ".[dev]"
```

This will install all the dependencies listed in the `pyproject.toml` file, including the optional development dependencies.

### 2.4. Install Node.js Dependencies

The WhatsApp bridge requires Node.js dependencies. Navigate to the `bridge` directory and install them using `npm`:

```bash
cd bridge
npm install
npm run build
cd ..
```

### 2.5. Run the Application

Once all dependencies are installed, you can run the application using the following command:

```bash
nanobot status
```

This will start the application and display its status.

## 3. Runtime Execution

For runtime execution, you have two primary options: running directly on the host machine or using Docker.

### 3.1. Host Machine Execution

To run the application directly on your host machine, follow these steps:

1. **Clone the Repository:**
   ```bash
   git clone <repository-url>
   cd <repository-directory>
   ```

2. **Install Python Dependencies:**
   ```bash
   pip install "uv>=0.1.0"
   uv pip install .
   ```

3. **Install Node.js Dependencies:**
   ```bash
   cd bridge
   npm install
   npm run build
   cd ..
   ```

4. **Run the Application:**
   ```bash
   nanobot status
   ```

### 3.2. Docker Execution

Using Docker is the recommended approach for a consistent and isolated runtime environment.

1. **Build the Docker Image:**
   From the root of the project directory, build the Docker image:
   ```bash
   docker build -t nanobot-ai .
   ```

2. **Run the Docker Container:**
   Run the application in a Docker container with the following command:
   ```bash
   docker run -p 18790:18790 nanobot-ai
   ```
   This will start the application and expose the default gateway port `18790`.

## 4. Configuration

The application requires a configuration file to be present in the `/root/.nanobot` directory. Create a `config.json` file in this directory with the necessary settings. Refer to the documentation for details on the available configuration options.

By following these instructions, you can successfully install and run the application for both development and runtime purposes.
