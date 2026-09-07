# Detector de Silencio — versión notificación push (sin mail)

Versión reducida de la app: monitorea una entrada o salida de audio y, si
detecta silencio sostenido, envía una **notificación push por ntfy.sh**
(sin necesidad de crear ninguna cuenta) y, opcionalmente, ejecuta un
programa y/o reproduce un audio de respaldo. No incluye nada de mail — esa
parte la vamos a retomar más adelante en la versión completa.

## 1. Requisitos

- Windows 10/11
- Python 3.10 o superior (https://www.python.org/downloads/), con "Add
  python.exe to PATH" tildado al instalar.

## 2. Instalación

```
pip install -r requirements.txt
```

## 3. Uso

```
python app.py
```

### Pestaña "Monitor"
Igual que en la versión completa: elegís **Salida (loopback)** o
**Entrada**, el dispositivo, calibrás el umbral con el medidor en vivo, y
definís cuánto tiempo de silencio antes de avisar. Además:

- **Iniciar el monitoreo automáticamente al abrir el programa**: útil si
  configurás Windows para que abra el programa solo al iniciar la PC (por
  ejemplo, después de un reinicio). Si activás la programación por
  horario (ver abajo), esa programación manda y esta opción se ignora.
- **Programación automática por horario**: prende y apaga el monitoreo
  solo, según:
  - **Días activos**: tildá los días de la semana en que querés que
    funcione esta programación.
  - **Franjas horarias**: agregá una o varias franjas (ej. 13:00 a 20:00,
    y 05:00 a 08:00) durante las cuales el monitoreo debe estar
    encendido. Fuera de esos horarios y días, el programa lo apaga solo
    — incluso si lo habías iniciado a mano. El programa revisa el horario
    cada 30 segundos, así que el cambio de estado no es instantáneo al
    segundo, pero sí prácticamente inmediato.
  - Las franjas que cruzan la medianoche (ej. 22:00 a 06:00) también
    funcionan.

### Pestaña "Configuración"
- **Preferencias → Guardar la configuración automáticamente al cerrar el
  programa**: desactivado por defecto (tenés que tocar "Guardar
  configuración" a mano). Si lo activás, al cerrar el programa se guarda
  solo, sin preguntar.
- **Notificación push (ntfy.sh)**: activala con el checkbox, elegí un
  nombre de canal (o generá uno aleatorio), probalo con "Enviar
  notificación de prueba", y compartí ese nombre de canal con quien
  quieras que también reciba el aviso (cada persona instala la app ntfy
  gratis y se suscribe a ese mismo nombre).
- **Acción automática**: ejecutar un programa/script y/o reproducir un
  audio de respaldo `.wav`, igual que en la versión completa.

## 4. Convertir en un .exe

Con la app ya probada y funcionando por `python app.py`, para generar un
ejecutable que no dependa de tener Python instalado:

```
pyinstaller --onefile --noconsole --name DetectorDeSilencio app.py
```

### Con ícono personalizado

Si tenés un archivo `.ico` y lo ponés en la misma carpeta que `app.py`
(por ejemplo, llamado `icono.ico`), el comando queda así:

```
pyinstaller --onefile --noconsole --noupx --name DetectorDeSilencio --icon=icono.ico --add-data "icono.ico;." app.py
```

- `--icon=icono.ico`: es el ícono que se ve en el archivo `.exe` en el
  Explorador de Windows.
- `--add-data "icono.ico;."`: empaqueta el `.ico` DENTRO del propio `.exe`
  para que la app lo pueda usar también como ícono de la ventana y de la
  barra de tareas mientras está abierta (si no hacés esto, el ícono se ve
  bien en el Explorador pero no en la ventana abierta).
- `--noupx`: evita que PyInstaller comprima el `.exe` con UPX. Ver la
  sección de antivirus más abajo — reduce bastante los falsos positivos.

Qué hace cada parte del comando:
- `--onefile`: empaqueta todo en un único archivo `.exe` (más cómodo para
  compartir, aunque tarda un poco más en abrir que la versión de carpeta).
- `--noconsole`: no abre una ventana negra de consola atrás de la app.
- `--name DetectorDeSilencio`: así se va a llamar el ejecutable final.

Esto genera varias carpetas (`build/`, `dist/`, y un archivo
`DetectorDeSilencio.spec`). El ejecutable que te interesa queda en:

```
dist\DetectorDeSilencio.exe
```

Ese archivo ya podés copiarlo a cualquier PC con Windows (10/11, misma
arquitectura de 64 bits) y ejecutarlo directamente, sin instalar Python ni
nada — es completamente autocontenido.

**Notas sobre el .exe:**
- La primera vez que Windows lo ejecute, es posible que Windows Defender
  SmartScreen muestre un aviso de "Windows protegió su PC" (pasa con
  cualquier .exe nuevo sin firma digital). Se soluciona tocando "Más
  información" → "Ejecutar de todas formas".
- El archivo `config.json` con tu configuración guardada se crea **en la
  misma carpeta donde está el `.exe`** la primera vez que guardes algo
  (la app ya está armada para detectar que corre como `.exe` compilado y
  guardar ahí, en vez de en la carpeta temporal que usa PyInstaller
  internamente y que se borra sola al cerrar el programa). Si copiás el
  `.exe` a otra PC y querés llevarte la configuración, copiá también ese
  `config.json` junto con él.
- Podés borrar las carpetas `build/` y el archivo `.spec` después de
  generar el `.exe`; no hacen falta para que funcione, son solo archivos
  intermedios del proceso de compilación.
- Si en el futuro cambiás algo del código y querés generar el `.exe` de
  nuevo, simplemente volvé a correr el mismo comando `pyinstaller`.

## 5. Windows Defender / antivirus — por qué puede saltar y cómo evitarlo

Es un problema real y bastante común con programas hechos con PyInstaller,
no es algo exclusivo de esta app. Pasan dos cosas distintas, que conviene
diferenciar:

**1. El aviso de SmartScreen ("Windows protegió su PC")** — esto NO es un
antivirus detectando un virus, es Windows avisando que el archivo es de un
publicador desconocido (no tiene firma digital). Es normal y esperable en
cualquier programa gratuito/independiente nuevo. Se resuelve con "Más
información" → "Ejecutar de todas formas", y para vos como desarrollador
no hay mucho más que hacer salvo pagar un certificado de firma de código
(ver más abajo).

**2. Un antivirus lo marca directamente como "virus" o "troyano"** — esto
sí puede pasar, y es casi siempre un **falso positivo**: los `.exe` hechos
con PyInstaller en modo `--onefile` se autoextraen en tiempo de ejecución,
un comportamiento que coincide con el de muchos programas maliciosos
reales, así que algunos antivirus heurísticos (que buscan "comportamientos
sospechosos" más que virus conocidos) lo marcan por las dudas.

Qué podés hacer para minimizarlo:
- **Usar `--noupx`** (ya lo agregué al comando de arriba): PyInstaller
  comprime el `.exe` con UPX por defecto si lo tenés instalado, y esa
  compresión es una de las señales que más dispara falsos positivos.
- **Revisarlo en VirusTotal antes de publicarlo**: subí el `.exe` a
  virustotal.com (gratis) — analiza el archivo con ~70 antivirus distintos
  a la vez. Es normal que 1 o 2 de los motores menos conocidos lo marquen
  por heurística aunque los grandes (Windows Defender, Kaspersky, etc.) lo
  den limpio. Podés compartir el link del resultado en tu comunidad para
  que la gente vea que no es un virus real.
- **Si Windows Defender específicamente lo marca**, podés enviarlo a
  Microsoft para que lo revisen en
  https://www.microsoft.com/en-us/wdsi/filesubmission — normalmente lo
  resuelven en un par de días si confirman que es un falso positivo.
- **La solución definitiva (pero paga)** es un certificado de firma de
  código (~USD 100-400/año, de proveedores como DigiCert o Sectigo). Firma
  digitalmente el `.exe` con tu identidad verificada, lo que hace que
  SmartScreen deje de mostrar el aviso (después de que el certificado
  genere algo de reputación) y baja mucho la tasa de falsos positivos de
  antivirus. Para una herramienta gratuita de comunidad probablemente no
  valga la pena el gasto, pero es la opción a considerar si esto crece
  mucho.
- Como alternativa intermedia, `--onedir` en vez de `--onefile` genera una
  carpeta con el `.exe` y sus archivos por separado (en vez de un único
  archivo autoextraíble), lo cual también reduce falsos positivos —
  a cambio, tenés que compartir la carpeta completa (comprimida en `.zip`,
  por ejemplo) en vez de un solo archivo.

En resumen: no es algo roto en tu programa, es un problema conocido del
ecosistema. Con `--noupx` y una revisión en VirusTotal antes de publicar
ya reducís el riesgo bastante.

## 6. Notas técnicas

- El nivel de audio se calcula como RMS convertido a dBFS sobre bloques de
  ~100 ms.
- El modo "Salida (loopback)" usa `soundcard` + `comtypes` (loopback
  nativo de WASAPI en Windows 10/11), por eso ambas están en
  `requirements.txt`.
- La notificación push usa `ntfy.sh` mediante HTTP, sin librerías extra
  (solo `urllib`, que viene con Python).
- La reproducción de audio de respaldo usa `winsound` (nativo de
  Windows), por eso solo funciona en Windows y solo con archivos `.wav`.
