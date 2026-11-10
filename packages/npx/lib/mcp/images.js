"use strict";

/**
 * Frame images for MCP `image` content blocks (design mcp-server 4.3).
 *
 * Pending items list their images as `{ path, url }` (local file, media endpoint
 * relative to the service). A chat client cannot read either, so the MCP shell
 * loads the bytes (local file first, media endpoint as fallback when the service
 * runs elsewhere) and returns them base64-encoded with the long side capped.
 *
 * Images already within the cap pass through byte for byte. Oversized JPEGs
 * (the only format the frame extractor writes) are decoded with jpeg-js, box
 * downscaled and re-encoded; other oversized formats are refused.
 */

const { readFile } = require("node:fs/promises");
const jpeg = require("jpeg-js");

const MAX_IMAGE_SIDE = 1280;
const JPEG_QUALITY = 85;

const MIME_BY_FORMAT = { jpeg: "image/jpeg", png: "image/png", webp: "image/webp", gif: "image/gif" };

/** { format, width, height } from the file header; width/height null when unknown. */
function imageInfo(buffer) {
  if (buffer.length >= 24 && buffer.readUInt32BE(0) === 0x89504e47) {
    return { format: "png", width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
  }
  if (buffer.length >= 4 && buffer[0] === 0xff && buffer[1] === 0xd8) {
    return { format: "jpeg", ...jpegSize(buffer) };
  }
  if (buffer.length >= 12 && buffer.toString("ascii", 0, 4) === "RIFF" && buffer.toString("ascii", 8, 12) === "WEBP") {
    return { format: "webp", width: null, height: null };
  }
  if (buffer.length >= 10 && buffer.toString("ascii", 0, 3) === "GIF") {
    return { format: "gif", width: buffer.readUInt16LE(6), height: buffer.readUInt16LE(8) };
  }
  return { format: null, width: null, height: null };
}

/** Frame size from the first SOFn segment of a JPEG. */
function jpegSize(buffer) {
  let offset = 2;
  while (offset + 9 < buffer.length) {
    if (buffer[offset] !== 0xff) {
      offset += 1;
      continue;
    }
    const marker = buffer[offset + 1];
    if (marker === 0xff) {
      offset += 1;
      continue;
    }
    if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) {
      offset += 2;
      continue;
    }
    const length = buffer.readUInt16BE(offset + 2);
    const isSof = marker >= 0xc0 && marker <= 0xcf && marker !== 0xc4 && marker !== 0xc8 && marker !== 0xcc;
    if (isSof) {
      return { height: buffer.readUInt16BE(offset + 5), width: buffer.readUInt16BE(offset + 7) };
    }
    offset += 2 + length;
  }
  return { width: null, height: null };
}

/** Area-average downscale of RGBA pixels. */
function downscale({ width, height, data }, targetWidth, targetHeight) {
  const out = Buffer.alloc(targetWidth * targetHeight * 4);
  const xRatio = width / targetWidth;
  const yRatio = height / targetHeight;
  for (let ty = 0; ty < targetHeight; ty += 1) {
    const y0 = Math.floor(ty * yRatio);
    const y1 = Math.max(y0 + 1, Math.min(height, Math.floor((ty + 1) * yRatio)));
    for (let tx = 0; tx < targetWidth; tx += 1) {
      const x0 = Math.floor(tx * xRatio);
      const x1 = Math.max(x0 + 1, Math.min(width, Math.floor((tx + 1) * xRatio)));
      let r = 0;
      let g = 0;
      let b = 0;
      for (let y = y0; y < y1; y += 1) {
        let index = (y * width + x0) * 4;
        for (let x = x0; x < x1; x += 1) {
          r += data[index];
          g += data[index + 1];
          b += data[index + 2];
          index += 4;
        }
      }
      const count = (y1 - y0) * (x1 - x0);
      const target = (ty * targetWidth + tx) * 4;
      out[target] = Math.round(r / count);
      out[target + 1] = Math.round(g / count);
      out[target + 2] = Math.round(b / count);
      out[target + 3] = 255;
    }
  }
  return { width: targetWidth, height: targetHeight, data: out };
}

/**
 * Fit an image into maxSide x maxSide. Returns { data (base64), mimeType, width,
 * height, resized }. Throws for unknown formats and oversized non-JPEG images.
 */
function fitImage(buffer, maxSide = MAX_IMAGE_SIDE) {
  const info = imageInfo(buffer);
  if (!info.format) {
    throw new Error("不支持的图片格式");
  }
  const mimeType = MIME_BY_FORMAT[info.format];
  const longSide = Math.max(info.width || 0, info.height || 0);
  if (info.width && info.height && longSide <= maxSide) {
    return { data: buffer.toString("base64"), mimeType, width: info.width, height: info.height, resized: false };
  }
  if (info.format !== "jpeg") {
    throw new Error(`图片超过 ${maxSide} 像素且不是 JPEG，无法压缩`);
  }
  const decoded = jpeg.decode(buffer, { useTArray: true, formatAsRGBA: true, maxResolutionInMP: 200 });
  const scale = maxSide / Math.max(decoded.width, decoded.height);
  if (scale >= 1) {
    return { data: buffer.toString("base64"), mimeType, width: decoded.width, height: decoded.height, resized: false };
  }
  const targetWidth = Math.max(1, Math.round(decoded.width * scale));
  const targetHeight = Math.max(1, Math.round(decoded.height * scale));
  const encoded = jpeg.encode(downscale(decoded, targetWidth, targetHeight), JPEG_QUALITY);
  return { data: encoded.data.toString("base64"), mimeType, width: targetWidth, height: targetHeight, resized: true };
}

/**
 * Load one item image `{ path, url }`: the local file when readable, otherwise
 * the media endpoint (`url` is relative to the service). Returns the raw bytes.
 */
async function loadImageBytes(image, { baseUrl, token }) {
  if (image && image.path) {
    try {
      return await readFile(image.path);
    } catch {
      // Not on this machine (service elsewhere) or already cleaned up: try the endpoint.
    }
  }
  if (image && image.url) {
    const target = new URL(image.url, `${baseUrl}/`).toString();
    const response = await fetch(target, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!response.ok) {
      throw new Error(`读取图片失败（${response.status}）`);
    }
    return Buffer.from(await response.arrayBuffer());
  }
  throw new Error("图片文件不存在");
}

module.exports = { fitImage, imageInfo, loadImageBytes, MAX_IMAGE_SIDE };
