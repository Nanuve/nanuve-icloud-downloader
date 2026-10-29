# Nanuve iCloud Downloader

App de escritorio para Windows que descarga tu fototeca de iCloud Photos
completa a una carpeta local, organizada automáticamente por día
(`AAAA/MM/DD`). Es una interfaz gráfica sobre
[icloudpd](https://github.com/icloud-photos-downloader/icloud_photos_downloader),
pensada para gente que no quiere tocar una línea de comandos.

![Nanuve iCloud Downloader](src/assets/nanuve_logo_2x.png)

## Características

- Descarga toda la fototeca (fotos y vídeos) o la reanuda si se corta a
  mitad: nunca vuelve a bajar un archivo que ya existe en destino.
- Barra de progreso real (no indeterminada): en cuanto icloudpd calcula el
  total, se muestra "1.234 de 12.345 (10%)".
- Verificación en dos pasos integrada: si tienes un teléfono registrado
  como número de confianza en tu Apple ID, puedes pedir que el código
  llegue por SMS en vez de a tus otros dispositivos Apple.
- No requiere tener Python instalado: es un único `.exe` para Windows.
- Evita que el equipo se suspenda mientras hay una descarga larga en curso.
- Tema oscuro consistente, incluida la barra de título de Windows.

## Descargar

La forma más rápida es bajar el `.exe` ya compilado desde la
[última release](../../releases/latest).

Al ser una app sin firmar digitalmente, es normal que Windows SmartScreen
avise la primera vez que la abras: pulsa "Más información" → "Ejecutar de
todas formas".

## Uso

1. Abre `Nanuve iCloud Downloader.exe`.
2. Escribe tu Apple ID y tu contraseña de iCloud, y elige la carpeta de
   destino (puede ser un disco externo).
3. Pulsa "Descargar fotos".
4. Si Apple pide el código de verificación en dos pasos, aparecerá una
   ventana para introducirlo (o pedirlo por SMS, si procede).

La contraseña solo se usa para conectar con iCloud en esa sesión y no se
guarda en ningún archivo. La sesión (cookies de `icloudpd`) sí se guarda
para no repetir el login cada vez, según el comportamiento habitual de
icloudpd.

## Compilar desde el código fuente

Requisitos: Python 3.10+ en Windows.

```powershell
python -m venv .venv
.venv\Scripts\pip install pyinstaller pillow

# icloudpd.exe: descarga el binario oficial de icloud-photos-downloader
# (https://github.com/icloud-photos-downloader/icloud_photos_downloader/releases)
# y colócalo en la raíz del proyecto como icloudpd.exe

.venv\Scripts\python -m PyInstaller `
  --name "Nanuve iCloud Downloader" `
  --onefile --windowed `
  --icon "src/assets/nanuve.ico" `
  --add-binary "icloudpd.exe;." `
  --add-data "src/assets/nanuve.ico;." `
  --add-data "src/assets/nanuve_logo_2x.png;." `
  --add-data "src/assets/HankenGrotesk-Regular.ttf;." `
  --add-data "src/assets/HankenGrotesk-SemiBold.ttf;." `
  --add-data "src/assets/HankenGrotesk-Bold.ttf;." `
  src/app.py
```

El ejecutable queda en `dist/`.

Durante el desarrollo también puedes ejecutar `python src/app.py`
directamente: busca los recursos en `src/assets/` y `icloudpd.exe` en la
raíz del proyecto sin necesidad de empaquetar nada.

## Créditos

- Motor de descarga: [icloud-photos-downloader](https://github.com/icloud-photos-downloader/icloud_photos_downloader) (MIT).
- Tipografía: [Hanken Grotesk](https://fonts.google.com/specimen/Hanken+Grotesk) (SIL Open Font License).

## Licencia

MIT para el código de este repositorio. `icloudpd.exe` se distribuye bajo
su propia licencia MIT original del proyecto icloud-photos-downloader.
