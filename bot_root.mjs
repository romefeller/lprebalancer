// The bot's root directory: the folder of this file. The global HALT lives
// here, so a script anywhere in the tree finds the same one.
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const BOT_ROOT = path.dirname(fileURLToPath(import.meta.url));
