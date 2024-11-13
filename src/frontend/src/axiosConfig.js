// axiosConfig.js
import axios from 'axios';

const api = axios.create({
  baseURL: 'https://3000-idx-growtoimpress-1730317223625.cluster-fnjdffmttjhy2qqdugh3yehhs2.cloudworkstations.dev',
  headers: {
    'Content-Type': 'application/json',
  },
});

export default api;

