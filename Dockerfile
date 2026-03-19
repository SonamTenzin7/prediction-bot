FROM python:3.10-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Create log file
RUN touch bot.log

# Run the bot
CMD ["python3", "-u", "bot.py"]
