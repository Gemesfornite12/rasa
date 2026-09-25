# Sara para OmniStudio (chat inicial con Rasa)

Este proyecto contiene el primer bot en español de OmniStudio y su Dockerfile para desplegarlo en Render.

## Esta primera etapa

- Chat de texto por REST.
- Respuestas básicas mediante intenciones y reglas de Rasa, sin Gemini.
- No cambia los módulos de imágenes, música, voz ni video de OmniStudio.
- Aún no responde libremente ni hace búsquedas web; eso se agregará por etapas.

## Despliegue en Render

1. En Render, crea un Blueprint desde `Gemesfornite12/rasa`, rama `3.6.x`.
2. Render leerá el `render.yaml` de la raíz y creará `sara-rasa` con el plan Free.
3. Guarda `RASA_AUTH_TOKEN` como secreto privado en Render; usa un valor largo y aleatorio. No lo pongas en GitHub ni lo compartas en un issue.
4. El Dockerfile entrena el bot durante la construcción y levanta el servidor REST.
5. La ruta para mensajes es `/webhooks/rest/webhook`; cada conversación lleva un `sender` único.

El servicio gratuito puede dormir tras un período sin tráfico y tardar en despertar. Esta primera versión no usa almacenamiento persistente, así que el contexto puede reiniciarse cuando el contenedor se suspenda o reinicie.

## Seguridad

El token es solo para pruebas privadas. No lo incrustes en una APK pública: se puede extraer. Antes de publicar OmniStudio para otros usuarios, cambiaremos a autenticación de Firebase validada en el servidor y pondremos límites de uso.
