╔═══════════════════════════════════════════════════════════════════════════════╗
║                                                                               ║
║              🐳 MICROVAULT - DOCKER DEPLOYMENT GUIDE 🐳                    ║
║                                                                               ║
║                     Complete Docker & Docker Compose Setup                   ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝

═════════════════════════════════════════════════════════════════════════════════

## 📦 QUICK START

### 1. Build and Run with Docker Compose (EASIEST)

```bash
# Clone/extract the project
cd microcred_integrated

# Copy environment file
cp .env.example .env

# Edit .env with your credentials
nano .env

# Start application
docker-compose up -d

# View logs
docker-compose logs -f microvault

# Stop application
docker-compose down
```

### 2. Access Application

```
http://localhost:5000
```

### 3. Check Status

```bash
docker-compose ps
docker-compose logs microvault
```

═════════════════════════════════════════════════════════════════════════════════

## 🏗️ MANUAL DOCKER BUILD

### Build Image

```bash
docker build -t microvault:latest .
```

### Run Container

```bash
docker run -d \
  --name microvault \
  -p 5000:5000 \
  -e FLASK_ENV=production \
  -e SECRET_KEY=your-secret-key \
  -e MAIL_USER=your-email@gmail.com \
  -e MAIL_PASS=your-app-password \
  -v $(pwd)/data:/app/instance \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/uploads:/app/static/uploads \
  microvault:latest
```

### View Logs

```bash
docker logs -f microvault
```

### Stop Container

```bash
docker stop microvault
docker rm microvault
```

═════════════════════════════════════════════════════════════════════════════════

## 🔧 ENVIRONMENT VARIABLES

Edit `.env` file before running:

```
# Essential
FLASK_ENV=production
SECRET_KEY=generate-a-secure-key

# Email (Gmail)
MAIL_USER=your-email@gmail.com
MAIL_PASS=your-app-password

# SMS (Twilio)
TWILIO_ACCOUNT_SID=your-sid
TWILIO_AUTH_TOKEN=your-token
TWILIO_PHONE=+1234567890
```

### Generate Secret Key

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

═════════════════════════════════════════════════════════════════════════════════

## 📁 VOLUMES & PERSISTENCE

### Docker Compose Creates:

```
data/                    # Database files
logs/                    # Application logs
uploads/                 # User uploads
```

### Mount Paths:

- `/app/instance` → `./data` (database)
- `/app/logs` → `./logs` (logs)
- `/app/static/uploads` → `./uploads` (uploads)

═════════════════════════════════════════════════════════════════════════════════

## 🌐 NETWORK & PORTS

### Exposed Ports:

```
5000 - Flask application
```

### Network:

```
microvault-network - Internal docker network
```

### Access from Host:

```
http://localhost:5000
```

═════════════════════════════════════════════════════════════════════════════════

## 🏥 HEALTH CHECKS

### Docker Compose Includes:

```
Interval: 30 seconds
Timeout: 10 seconds
Retries: 3
Start Period: 40 seconds
```

### Manual Check:

```bash
curl http://localhost:5000/health
```

═════════════════════════════════════════════════════════════════════════════════

## 📊 MONITORING

### View Logs

```bash
# Docker Compose
docker-compose logs microvault
docker-compose logs -f microvault           # Follow logs
docker-compose logs --tail 100 microvault   # Last 100 lines

# Direct Docker
docker logs microvault
docker logs -f microvault
```

### View Processes

```bash
docker-compose ps
docker ps | grep microvault
```

### View Resource Usage

```bash
docker stats microvault
```

### Enter Container

```bash
docker-compose exec microvault bash
# or
docker exec -it microvault bash
```

═════════════════════════════════════════════════════════════════════════════════

## 💾 DATABASE BACKUP

### Backup

```bash
docker-compose exec microvault cp /app/microcred.db /app/instance/backup-$(date +%Y%m%d).db
```

### Backup to Host

```bash
docker cp microvault:/app/instance/microcred.db ./backups/microcred-$(date +%Y%m%d).db
```

═════════════════════════════════════════════════════════════════════════════════

## 🚀 PRODUCTION DEPLOYMENT

### Use Nginx + Docker

Create `nginx.conf`:

```nginx
upstream app {
    server microvault:5000;
}

server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /static {
        alias /app/static;
    }
}
```

### Enable SSL with Let's Encrypt

```bash
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v ./certs:/certs \
  certbot/certbot certonly --standalone \
  -d your-domain.com
```

### Update docker-compose.yml for production:

```yaml
ports:
  - "80:80"
  - "443:443"
```

═════════════════════════════════════════════════════════════════════════════════

## 🔄 UPDATING THE APPLICATION

### Method 1: Rebuild Image

```bash
docker-compose down
docker-compose up -d --build
```

### Method 2: Pull New Code

```bash
git pull origin main
docker-compose up -d --build
```

═════════════════════════════════════════════════════════════════════════════════

## 🛠️ TROUBLESHOOTING

### Container Won't Start

```bash
docker-compose logs microvault
# Check error message
```

### Port Already in Use

```bash
# Change port in docker-compose.yml
ports:
  - "8000:5000"  # Use 8000 instead
```

### Database Issues

```bash
# Reset database
docker-compose exec microvault rm /app/instance/microcred.db
docker-compose restart microvault
```

### Out of Memory

```bash
# Increase resource limits in docker-compose.yml
services:
  microvault:
    deploy:
      resources:
        limits:
          memory: 1G
        reservations:
          memory: 512M
```

═════════════════════════════════════════════════════════════════════════════════

## 📋 USEFUL COMMANDS

```bash
# Start
docker-compose up -d

# Stop
docker-compose down

# Restart
docker-compose restart

# View logs
docker-compose logs -f

# Execute command
docker-compose exec microvault bash

# Remove all
docker-compose down -v

# Rebuild
docker-compose build --no-cache

# Pull latest image
docker-compose pull

# Scale (multiple instances)
docker-compose up -d --scale microvault=3
```

═════════════════════════════════════════════════════════════════════════════════

## 🔐 SECURITY BEST PRACTICES

1. **Generate Strong Secret Key**
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

2. **Use Environment Variables**
   - Never commit `.env` to git
   - Use `.env.example` as template

3. **Keep Image Updated**
   ```bash
   docker pull python:3.9-slim
   docker-compose build --no-cache
   ```

4. **Use Health Checks**
   - Already included in docker-compose.yml

5. **Enable HTTPS**
   - Use Nginx reverse proxy with SSL

6. **Limit Resources**
   ```yaml
   deploy:
     resources:
       limits:
         cpus: '1'
         memory: 1G
   ```

═════════════════════════════════════════════════════════════════════════════════

## 📚 FILES INCLUDED

- `Dockerfile` - Container image definition
- `docker-compose.yml` - Multi-container orchestration
- `.dockerignore` - Files to ignore in Docker build
- `DOCKER_DEPLOYMENT.md` - This guide

═════════════════════════════════════════════════════════════════════════════════

## ✅ STATUS

✅ Docker setup complete
✅ Docker Compose ready
✅ Health checks included
✅ Volume persistence configured
✅ Environment variables set
✅ Production ready

═════════════════════════════════════════════════════════════════════════════════

Generated: May 19, 2025
Status: ✅ READY FOR DEPLOYMENT
