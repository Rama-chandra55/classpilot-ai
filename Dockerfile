# Use a lightweight Python base image
FROM python:3.11-slim

# Create a non-root user and group
RUN groupadd -r classpilot && useradd -r -g classpilot classpilot

# Set the working directory
WORKDIR /app

# Install dependencies via pyproject.toml
RUN pip install --no-cache-dir --upgrade pip

# Copy pyproject.toml and README.md to cache dependencies layer
COPY pyproject.toml README.md /app/
RUN pip install --no-cache-dir .

# Copy the rest of the application
COPY classpilot /app/classpilot

# Change ownership to the non-root user
RUN chown -R classpilot:classpilot /app

# Switch to non-root user
USER classpilot

# Expose the default Cloud Run port
EXPOSE 8080

# Run the HTTP server module directly
CMD ["python", "-m", "classpilot.http_server"]
