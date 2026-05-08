# Multi-Agent Studio - Base Sandbox Image
# Pre-installed with OpenCode CLI + Git + Python3 + GCC + Node.js
#
# Directory layout:
#   /workspace       - Agent work tree (no .git, Agent cannot destroy checkpoints)
#   /sandbox-meta    - Git metadata directory (only Python control layer operates this)
#   /workspace/.workflow   - Inter-node context sharing directory
#   /workspace/.opencode  - OpenCode stream output directory

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    wget \
    build-essential \
    gcc \
    g++ \
    python3 \
    python3-pip \
    python3-venv \
    nodejs \
    npm \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Install Bun (OpenCode runtime)
RUN curl -fsSL https://bun.sh/install | bash
ENV PATH="/root/.bun/bin:${PATH}"

# Install OpenCode CLI (npm package: opencode-ai)
RUN bun install -g opencode-ai@latest

# Create sandbox directory structure
RUN mkdir -p /workspace/.workflow \
    && mkdir -p /workspace/.opencode \
    && mkdir -p /sandbox-meta/.git \
    && mkdir -p /root/.opencode

# Set workspace as default directory
WORKDIR /workspace

# Default command (overridden by orchestrator)
CMD ["bash"]
