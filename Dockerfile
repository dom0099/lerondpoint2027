FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ffmpeg — première dépendance SYSTÈME du projet (chantier Vidéo, VIDEO-1). Elle ne
# s'installe pas par pip : `app/services/video.py` appelle les binaires `ffmpeg` et
# `ffprobe`, et sans eux le transcodage lève `OutilAbsent`. `--no-install-recommends`
# évite d'embarquer la moitié d'un bureau graphique avec.
#
# **Le jour du déploiement, le VPS de production devra recevoir le même paquet** —
# soit par cette image si l'API y est reconstruite, soit par `apt install ffmpeg` si
# elle tourne hors conteneur. Ce n'est pas fait dans ce lot : rien n'est déployé.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Les dépendances d'abord, pour que le cache de build survive aux changements de code.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir -e ".[dev]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
