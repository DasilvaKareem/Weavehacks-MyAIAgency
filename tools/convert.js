const obj2gltf = require('obj2gltf');
const fs = require('fs');
const path = require('path');
const inDir = 'assets/buildings/Models with Materials/OBJ';
const outDir = 'assets/buildings/glb';
const objs = fs.readdirSync(inDir).filter(f => f.toLowerCase().endsWith('.obj'));
(async () => {
  let ok = 0, fail = 0;
  for (const f of objs) {
    const inPath = path.join(inDir, f);
    const outPath = path.join(outDir, f.replace(/_Mat\.obj$/i, '').replace(/\.obj$/i, '') + '.glb');
    try {
      const glb = await obj2gltf(inPath, { binary: true });
      fs.writeFileSync(outPath, glb);
      ok++;
    } catch (e) {
      console.error('FAIL', f, e.message);
      fail++;
    }
  }
  console.log(`converted ${ok}/${objs.length} (fail ${fail})`);
})();
