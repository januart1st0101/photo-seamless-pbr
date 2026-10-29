#!/usr/bin/env python3
"""Build aligned 4K texture maps from an already seamless, corrected base color."""
import argparse
import zipfile
from pathlib import Path
import numpy as np
from PIL import Image, ImageFilter

SIZE = 4096

def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--material', choices=['opaque', 'transparent'], required=True)
    parser.add_argument('--transmission', type=float, help='User-specified transmission from 0 to 1')
    args = parser.parse_args()
    if args.transmission is not None and not 0 <= args.transmission <= 1:
        parser.error('--transmission must be between 0 and 1')
    if args.material == 'opaque' and args.transmission not in (None, 0):
        parser.error('opaque material requires zero transmission')
    return args

def blur_periodic(data, radius):
    pad = int(radius * 4 + 4)
    byte = np.asarray(np.clip(data * 255 + .5, 0, 255), dtype=np.uint8)
    arr = np.pad(byte, pad, mode='wrap')
    result = Image.fromarray(arr).filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(result, dtype=np.float32)[pad:-pad, pad:-pad] / 255

def gray(data, target):
    Image.fromarray(np.asarray(np.clip(data * 255 + .5, 0, 255), dtype=np.uint8)).save(target)

def main():
    args = parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit('Output directory must be empty; refusing to overwrite existing files.')
    archive = args.output.with_name(args.output.name + '.zip')
    if archive.exists():
        raise SystemExit('ZIP already exists; refusing to overwrite it.')
    with Image.open(args.source) as source:
        source = source.convert('RGB')
        orig = source.size
        if orig[0] != orig[1]:
            raise SystemExit('Source must be square; prepare it without distorting the material.')
        base = source.resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    args.output.mkdir(parents=True, exist_ok=True)
    base.save(args.output / '01_BaseColor_sRGB.png')
    # Periodic gradients and blur avoid treating outside image bounds as black.
    lum = np.asarray(base.convert('L'), dtype=np.float32) / 255
    mid = blur_periodic(lum, 8)
    detail = lum - mid
    height = np.clip(.5 + 1.6 * detail, 0, 1)
    del mid, detail
    height = blur_periodic(height, .8)
    Image.fromarray(np.asarray(np.clip(height * 65535 + .5, 0, 65535), dtype=np.uint16)).save(args.output / '05_Height_16bit.png')
    dx = (np.roll(height, -1, 1) - np.roll(height, 1, 1)) * 2
    dy = (np.roll(height, -1, 0) - np.roll(height, 1, 0)) * 2
    length = np.sqrt(dx * dx + dy * dy + 1)
    normal = np.empty((SIZE, SIZE, 3), dtype=np.uint8)
    normal[:, :, 0] = np.asarray(np.clip((-.5 * dx / length + .5) * 255 + .5, 0, 255), dtype=np.uint8)
    normal[:, :, 1] = np.asarray(np.clip((.5 * dy / length + .5) * 255 + .5, 0, 255), dtype=np.uint8)
    normal[:, :, 2] = np.asarray(np.clip((.5 / length + .5) * 255 + .5, 0, 255), dtype=np.uint8)
    Image.fromarray(normal).save(args.output / '02_Normal_OpenGL.png')
    normal[:, :, 1] = 255 - normal[:, :, 1]
    Image.fromarray(normal).save(args.output / '02_Normal_DirectX.png')
    del dx, dy, length, normal
    gray(np.clip(.62 - .2 * (height - .5), .35, .9), args.output / '03_Roughness.png')
    local = blur_periodic(height, 4)
    gray(np.clip(1 - np.maximum(local - height, 0) * 1.5, .65, 1), args.output / '04_AO.png')
    del local, height, lum
    Image.new('L', (SIZE, SIZE), 255).save(args.output / '06_Specular_Weight_Neutral.png')
    transmission = args.transmission if args.material == 'transparent' else 0
    if transmission is not None:
        Image.new('L', (SIZE, SIZE), round(transmission * 255)).save(args.output / '07_Refraction_Transmission.png')
    tile = base.resize((512, 512), Image.Resampling.LANCZOS)
    preview = Image.new('RGB', (1536, 1536))
    for y in range(3):
        for x in range(3): preview.paste(tile, (512*x, 512*y))
    preview.save(args.output / '08_同方向拼接預覽.png')
    transmission_note = (f'{transmission:.3f}' if transmission is not None else '未提供，因此未生成透光貼圖')
    note = f'''4K 貼圖製作說明\n\nBase Color 來源尺寸：{orig[0]} × {orig[1]}；輸出尺寸：4096 × 4096。若來源低於 4K，輸出是插值放大，不代表新增真實細節。Base Color 使用 sRGB，其他貼圖請設為 Raw／Linear。\nNormal 提供 OpenGL（Y+）和 DirectX（Y−）；依渲染器選擇。Roughness 白色較粗糙；Height 16-bit 白色較高；AO 白色較少遮蔽。上述資料由顏色近似推估，並非實測。\nSpecular 是全白中性反射權重，並非相片中高光的量測；實際渲染器可直接使用預設反射權重與 IOR，避免重複調制。\nRefraction/Transmission 的均勻透光權重：{transmission_note}，非折射率 IOR 貼圖。{'不透明材料的預設透光量為 0。' if args.material == 'opaque' else ('透光量由使用者提供。' if transmission is not None else '請提供透光量後再生成。')} IOR 必須另行設定，不由照片猜測。\n預覽為同方向 3×3 拼貼，不是 3D 渲染；相同紋理仍可能產生重複感。未自動修復接縫或測試 Cinema 4D。\n'''
    (args.output / '使用說明.txt').write_text(note, encoding='utf-8')
    # Verify map dimensions before packaging; the preview intentionally has a different size.
    for path in sorted(args.output.glob('*.png')):
        if path.name.startswith('08_'):
            continue
        with Image.open(path) as image:
            if image.size != (SIZE, SIZE):
                raise SystemExit(f'Unexpected texture dimensions: {path}')
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(args.output.iterdir()):
            if path.is_file():
                bundle.write(path, arcname=path.name)
    with zipfile.ZipFile(archive, 'r') as bundle:
        broken = bundle.testzip()
        if broken:
            raise SystemExit(f'ZIP integrity check failed: {broken}')
    print(f'輸出：{args.output}，來源：{orig[0]} × {orig[1]}，各材質貼圖：4096 × 4096，ZIP：{archive}')

if __name__ == '__main__':
    main()
