# Registro de cambios de CoreBot

Este archivo documenta los cambios del proyecto CoreBot. Los plugins conservan
su propio número de versión.

## v1.3.0 — Actualizaciones posteriores a la etiqueta

La versión de referencia del proyecto es `v1.3.0`. Esta sección registra
los cambios incorporados **después** de esa etiqueta en la rama
`feature/init-bot`, hasta el commit `e50b70f`.

### Añadido

- **CRM (1.0.0):** generación del informe gerencial semanal, con resumen de
  interacciones y entrega de un informe en PDF por correo mediante Gmail
  cuando el envío está configurado.
- **Cotizador (1.1.1):** generación de cotizaciones en PDF a solicitud del
  cliente y derivación a un asesor para coordinar el pago.

### Cambiado

- **Citas (1.6.1):** solicitud en laboratorio o a domicilio; la solicitud a
  domicilio queda sujeta a confirmación de un asesor. Se incorpora la
  descripción proporcionada por el usuario a la solicitud de cita.
- **Menú de Telegram (1.4.0):** navegación guiada con botones, pantallas fijas
  que no requieren llamadas al modelo y recuperación de evidencia para las
  opciones informativas guiadas.
- **Atención humana (2.1.0):** los temas privados de atención se identifican
  con el nombre de Telegram del cliente y el identificador del caso.

### Operación

- Configurar en cada entorno las credenciales de Telegram, Calendar y Gmail.
  Conservar las bases operativas, los tokens y los respaldos fuera del repositorio.
- Tras la instalación, verificar desde una cuenta de prueba la consulta de
  cupos y la confirmación de citas, la descarga del PDF de cotización, la
  derivación y el envío del informe. El informe semanal y el panel web local
  son componentes distintos; esta versión documenta el informe semanal.

[Comparar con la etiqueta v1.3.0](https://github.com/Stalyn-1994/agent-bot/compare/v1.3.0...feature/init-bot)
