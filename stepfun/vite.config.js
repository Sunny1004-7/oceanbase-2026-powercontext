import {defineConfig} from 'vite';
export default defineConfig({build:{target:'esnext'},worker:{format:'es'},server:{host:'0.0.0.0'}});
