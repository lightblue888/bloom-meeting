// The main window's marks — @blurt/icons glyphs: the sidebar (rail) button.
import { Icon } from '@blurt/icons';
import type { CSSProperties } from 'react';

type P = { size?: number; className?: string; title?: string; style?: CSSProperties };

export const SidebarGlyph = ({ size = 16, className, title, style }: P) => <Icon name="sidebar" size={size} className={className} title={title} style={style} />;
