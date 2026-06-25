import type { ThemeTypes } from '@/types/themeTypes/ThemeType';

const PurpleTheme: ThemeTypes = {
  name: 'PurpleTheme',
  dark: false,
  variables: {
    'border-color': '#e53935',
    'carousel-control-size': 10
  },
  colors: {
    primary: '#d32f2f',
    secondary: '#212121',
    info: '#00bcd4',
    success: '#4caf50',
    accent: '#ff5252',
    warning: '#fb8c00',
    error: '#ff5252',
    lightprimary: '#ffebee',
    lightsecondary: '#f5f5f5',
    lightsuccess: '#e8f5e9',
    lighterror: '#ffebee',
    lightwarning: '#fff3e0',
    primaryText: '#212121',
    secondaryText: '#000000aa',
    darkprimary: '#b71c1c',
    darksecondary: '#000000',
    borderLight: '#e0e0e0',
    border: '#e0e0e0',
    inputBorder: '#9e9e9e',
    containerBg: '#fafafa',
    surface: '#fff',
    'on-surface-variant': '#fff',
    facebook: '#4267b2',
    twitter: '#1da1f2',
    linkedin: '#0e76a8',
    gray100: '#fafafacc',
    primary200: '#ef9a9a',
    secondary200: '#b0bec5',
    background: '#ffffff',
    overlay: '#ffffffaa',
    codeBg: '#ececec',
    preBg: 'rgb(249, 249, 249)',
    code: 'rgb(13, 13, 13)',
    chatMessageBubble: '#f5f5f5',
    mcpCardBg: '#ffebee',
  }
};

export { PurpleTheme };
