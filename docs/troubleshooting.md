# Troubleshooting

## Common Issues

### Books not showing up?
- **Check Volumes**: Ensure your `/books` volume is correctly mounted in `docker-compose.yml`. The path inside the container must match where you are looking.
- **Permissions**: Ensure the user running the container has read permissions for the ebook files.

### Transcription taking too long?
- **Model Size**: Try setting `WHISPER_MODEL=tiny` in the Settings page.
- **Hardware**: Transcription is CPU-intensive. If possible, enable [GPU Acceleration](#gpu-acceleration-optional).

### Syncing backwards?
The system includes anti-regression logic, but if you switch devices rapidly, issues can occur.
- **Solution**: Go to the Dashboard and click **"Reset Progress"** for the affected book. This clears the stored sync state without affecting your external accounts.

---

## Logs

Documentation and live logs are available directly in the Web UI.
Alternatively, you can view them via the terminal:

```bash
docker compose logs -f
```

Look for lines starting with `[INFO]` or `[ERROR]`.

---

## GPU Acceleration

See the **[Configuration Guide](configuration.md#gpu-support-optional)** for instructions on enabling NVIDIA GPU acceleration.
