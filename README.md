# Trackmania Skin Converter

A fast, single-file texture converter for **Trackmania (2020) Stadium car skins**.

Drop in your painted textures (PNG, TGA, JPG) and get a ready-to-upload skin `.zip`: every
texture compressed to the DDS format the game expects, with mipmaps, plus `Icon.tga`.
It also works the other way round: open any skin zip or DDS file and extract the textures
losslessly as PNG or TGA to edit them.

**No install. Nothing is uploaded.** It's one HTML file that runs completely in your browser.

## Usage

**▶ Use it online: https://lollpdesu.github.io/trackmania-skin-converter/**
(or download [`skin_converter.html`](skin_converter.html) and open it offline, it's the same file).

1. Open the page.
2. Drop your textures, a folder, or an existing skin `.zip` onto the window.
   Files are matched to slots by name (`Skin_B.png`, `MyCar_Details_N.tga`, ...).
   You can also drop a file directly onto a slot.
3. Click **Export Skin** and save the zip to
   `Documents\Trackmania\Skins\Models\CarSport`.
4. In game: your club → **Skins** → **Upload a skin**.

Use **PNG** / **TGA** in the header to extract every loaded texture instead.

## Texture slots

| Slot | Format | Content |
|---|---|---|
| `Skin_B` | BC1 | Base colour |
| `Skin_R` | BC5 | Roughness (R) · metalness (G) |
| `Skin_CoatR` | BC4 | Clear coat / glitter (optional) |
| `Skin_DirtMask` | BC4 | Dirt mask |
| `Details_B` | BC1 | Base colour |
| `Details_R` | BC5 | Roughness · metalness |
| `Details_I` | BC3 | Self-illumination (RGB + alpha) |
| `Details_N` | BC5 | Normal map (OpenGL, Y+) |
| `Details_DirtMask` | BC4 | Dirt mask |
| `Wheels_B` | BC1 | Base colour |
| `Wheels_R` | BC5 | Roughness · metalness |
| `Wheels_N` | BC5 | Normal map |
| `Wheels_DirtMask` | BC4 | Dirt mask |
| `Glass_D` | BC1 | Tint (colour) · opacity (brightness) |
| `Glass_I` | BC3* | Self-illumination |
| `Icon` | TGA | Skin icon |

Formats follow the official Stadium car skin guide and are written with D3D9-style headers
(`DXT1`, `DXT5`, `ATI1`, `ATI2`). \*The guide lists BC5 for `Glass_I` but describes RGB + alpha,
which BC5 can't store, so BC3 is the default. Every slot's format can be changed in the app.

## Features

- BC1 / BC3 / BC4 / BC5 encoder (PCA + least-squares refinement), multi-threaded with Web Workers.
  A full skin of 2048² textures with mipmaps encodes in about 2 seconds.
- Gamma-correct mipmaps for colour maps, renormalised mipmaps for normal maps.
- Imported DDS files that already have the right format are reused as-is (no double compression).
- Preview any channel and the compressed result (with PSNR) before exporting.
- Reads DDS (BC1–BC5, BC7, uncompressed), PNG (all bit depths, interlaced), TGA (incl. RLE), JPG and zip.
- Lossless PNG / TGA output.

Works in current Chrome, Edge, Firefox and Safari. In Chrome / Edge you can save the zip
straight into the skins folder; other browsers download it normally.

## Command-line extra

[`dds_convert.py`](dds_convert.py) is a standalone, dependency-free Python 3 tool for
lossless DDS → PNG / TGA conversion (BC1–BC5, BC7 and uncompressed formats).

```
python dds_convert.py texture.dds
python dds_convert.py -f both -o out path\to\textures
```

On Windows you can also drag `.dds` files or folders onto `DDS to PNG.bat` / `DDS to TGA.bat`.

## Resources

UV maps, the 3D model and baked maps are in the official
[Stadium car resources thread](https://devtrackers.gg/trackmania/p/3490d99c-stadium-car-ressources-all-you-need-to-create-skins-for-the-stadium-cars).

## License

[MIT](LICENSE). This is a fan-made tool, not affiliated with or endorsed by Ubisoft or Nadeo.
Trackmania is a trademark of Ubisoft Entertainment.
