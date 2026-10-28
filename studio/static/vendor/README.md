# Bundled Three.js runtime

Pinned upstream package: `three@0.186.1` from https://registry.npmjs.org/three/0.186.1.
Upstream project: https://github.com/mrdoob/three.js. MIT license retained in THREE-LICENSE.txt.
Built with `esbuild@0.28.2`; no runtime CDN or package installation is required.

Entry:

```js
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
window.THREE = Object.assign({}, THREE, { GLTFLoader });
```

Build: `esbuild entry.js --bundle --format=iife --platform=browser --target=es2020 --minify --legal-comments=inline --outfile=studio-three.js`

SHA-256: `2b54aaeb5e7bb8b9c62b1b98b82ea621d08fb12e754ea08bc83c5801652aecc6`.

Generated source may use global THREE and THREE.GLTFLoader. The server inlines this bundle when needed and embeds approved revision assets as data URLs; arbitrary external network access remains blocked. GLB inputs must be self-contained and not require external sidecar files or remote decoder services.
