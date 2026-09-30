# Dataset multimedia de pruebas

Generado: 2026-09-29T22:37:28.235086+00:00 (semilla 42).

## Composicion

| Concepto | Valor |
|---|---|
| Archivos | 480 |
| Videos | 288 |
| Audios | 192 |
| Volumen total | 655.7 MB (687533260 bytes) |
| Archivos problematicos | 14 |

| Formato | Archivos |
|---|---|
| avi | 53 |
| flac | 38 |
| m4a | 38 |
| mkv | 58 |
| mov | 55 |
| mp3 | 39 |
| mp4 | 68 |
| ogg | 38 |
| wav | 39 |
| webm | 54 |

| Clase de tamano | Archivos |
|---|---|
| heavy | 70 |
| light | 246 |
| medium | 164 |

Clases: video light 2-5 s a 320x240, medium 10-20 s a 640x360, heavy 30-45 s a 1280x720; audio light 3-8 s, medium 15-30 s, heavy 60-120 s. Los medios se sintetizan con FFmpeg (testsrc, testsrc2, smptebars, mandelbrot, sine, anoisesrc).

## Metadatos y estructura

- `media/<evento>/<archivo>`: archivos; la clave en MinIO es la ruta relativa a `media/`.
- `manifest.json`: metadatos por archivo (`event`, `session`, `user`, `batch`, formato, clase de tamano, duracion, resolucion, bytes, `problematic`).
- `cases.json`: casos generados automaticamente a partir de los metadatos.

## Criterios de agrupacion

- Casos homogeneos (`batch`): 73 casos. Un lote de ingesta contiene un solo tipo; videos con `transcode_video` (algunos lotes con `generate_thumbnail`) y audios con `convert_audio`.
- Casos heterogeneos (`event+session`): 57 casos. Mezclan audio y video, rotando operaciones validas para cada tipo.
- Casos por usuario (`user`): 5 casos con `task_type: "auto"`.
- Total: 135 casos (73 homogeneos y 62 heterogeneos, contando los 5 casos por usuario como heterogeneos), 999 sub-tareas, de 5 a 11 por caso (el generador admite de 3 a 15).
- Los archivos problematicos (bytes aleatorios, mp4 sin video, mp4 sin audio, ~3 %) van incluidos para que aparezcan casos `partially_completed`.

## Regenerar

```bash
docker run --rm -v "$PWD/dataset:/app/dataset" <imagen-worker> \
    python -m scripts.build_dataset --out dataset --files 480 --seed 42
```

Cargar y ejecutar: `python -m scripts.run_load --dataset dataset --upload`.
