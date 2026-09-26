# Plataforma Distribuida de Procesamiento Multimedia

Sistema distribuido basado en microservicios y arquitectura orientada a colas para el procesamiento concurrente de archivos multimedia (video/audio), desarrollado con FastAPI, Redis y Python.

## Librerías Utilizadas

El proyecto utiliza las siguientes dependencias principales en el ecosistema de Python:

* **FastAPI**: Framework web moderno y de alto rendimiento para construir el nodo coordinador y sus endpoints REST.
* **Uvicorn**: Servidor ASGI rápido para ejecutar la aplicación de FastAPI.
* **Redis (PyRedis)**: Cliente oficial de Python para la gestión de colas de tareas y almacenamiento clave-valor.
* **Requests**: Librería HTTP para que los nodos worker reporten los resultados de vuelta al coordinador.
* **Pydantic**: Validación de datos y esquemas tipados para las peticiones y respuestas.

## Integración con Redis

Para la comunicación y distribución de tareas entre el coordinador y los workers, se utiliza un broker de mensajes basado en Redis.

### Pasos para levantarlo localmente con Docker:
1. Asegúrate de tener Docker Desktop instalado y abierto en tu computadora.
2. Descarga y ejecuta un contenedor oficial de Redis usando la imagen ligera de Alpine:
```bash
   docker run -d --name redis-server -p 6379:6379 redis:alpine
```
3. O bien, búscalo directamente en la interfaz gráfica de Docker Desktop (redis:alpine o redis:latest) y asegúrate de mapear el puerto 6379:6379.

### Cómo Instalar el Entorno

1. Clona o abre el repositorio del proyecto en tu máquina local.

2. Instala las dependencias necesarias ejecutando el siguiente comando en tu terminal:
```bash
    pip install fastapi uvicorn redis requests pydantic
```


### Cómo Ejecutar el Proyecto

El sistema se divide en dos componentes principales: el Coordinador y los Workers.

1. Ejecutar el Nodo Coordinador

El coordinador se encarga de recibir los casos, encolar las sub-tareas y gestionar la sincronización. Para levantarlo desde la raíz del proyecto, ejecuta:
```bash
    python -m uvicorn src.coordinator.main:app --reload --host 0.0.0.0 --port 8000
```

- Una vez encendido, puedes acceder a la documentación interactiva (Swagger UI) en tu navegador ingresando a: 
     http://localhost:8000/docs

2. Ejecutar los Nodos Workers (Persona 2 y Persona 3)

Los workers se conectan al servidor de Redis para extraer tareas pendientes y procesarlas.

1. En la máquina o terminal correspondiente al worker, asegúrate de tener el script worker.py.

2. Configura la IP del coordinador y de Redis en el script.

3. Ejecuta el worker:
```bash
    python worker.py
```