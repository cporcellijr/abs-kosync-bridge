# Troubleshooting

## Common Issues

### Books not showing up?

- **Check Volumes**: Ensure your `/books` volume is correctly mounted in `docker-compose.yml`. The path inside the container must match where you are looking.
- **Permissions**: Ensure the user running the container has read permissions for the ebook files.

### Transcription taking too long?

- **Model Size**: Try setting `WHISPER_MODEL=tiny` in the Settings page.
- **Hardware**: Transcription is CPU-intensive. If possible, enable [GPU Acceleration](#gpu-acceleration-optional).

### KOSync Port Not Working

- **Issue**: You set `KOSYNC_PORT` but cannot connect on that port.
- **Solution**: Ensure you have mapped the port in your `docker-compose.yml`.
  - Example: `ports: - "5758:5758"` if `KOSYNC_PORT=5758`.

### WhisperCpp Model Ignored

- **Issue**: WhisperCpp seems to use 'large-v3' even if I select 'small' in the UI.
- **Solution**: Previous versions had a bug where the model parameter wasn't sent. This is fixed in the latest release.
  - Ensure `WHISPER_MODEL` is set in your environment variables (e.g., `WHISPER_MODEL=small`).
  - Check the logs to see the request URL and data being sent.

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
