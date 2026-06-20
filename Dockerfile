FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Install essential packages and system dependencies needed for Playwright Chromium
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    procps \
    unzip \
    curl \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create user with UID 1000 required by Hugging Face Spaces
RUN useradd -m -u 1000 user
ENV PATH="/home/user/.local/bin:$PATH"

# Copy requirements and install dependencies
COPY --chown=user:user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY --chown=user:user . .

# Create a data directory with proper permissions
RUN mkdir -p /app/data && chmod 777 /app/data && chown -R user:user /app

# Switch to the non-root Hugging Face user
USER user

# Install Playwright Chromium browser
RUN python -m playwright install chromium

# Set default port to 7860 for Hugging Face Spaces
ENV PORT=7860
EXPOSE 7860

# Command to run the application
CMD ["python", "app.py"]