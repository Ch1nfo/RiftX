declare namespace chrome {
  namespace devtools {
    namespace panels {
      function create(title: string, iconPath: string, pagePath: string): void;
    }
    namespace network {
      const onRequestFinished: {
        addListener(callback: (entry: RiftXHarEntry) => void): void;
      };
    }
  }
  namespace tabs {
    function create(options: { url: string }): Promise<unknown>;
  }
}

interface RiftXHarHeader {
  name: string;
  value: string;
}

interface RiftXHarEntry {
  _resourceType?: string;
  request: {
    method: string;
    url: string;
    httpVersion?: string;
    headers: RiftXHarHeader[];
    postData?: { text?: string; encoding?: string };
  };
  response: {
    status: number;
    statusText?: string;
    httpVersion?: string;
    headers: RiftXHarHeader[];
    content?: { mimeType?: string };
  };
  startedDateTime?: string;
  getContent(callback: (content: string, encoding: string) => void): void;
}
