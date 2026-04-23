"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
const node_util_1 = require("node:util");
const node_stream_1 = require("node:stream");
const html_formatter_1 = require("@cucumber/html-formatter");
const message_streams_1 = require("@cucumber/message-streams");
exports.default = {
    type: 'formatter',
    formatter({ on, options, write, directory }) {
        if (!directory && options.externalAttachments) {
            throw new Error('Unable to externalise attachments when formatter is not writing to a file');
        }
        const externaliseStream = new message_streams_1.AttachmentExternalisingStream({
            behaviour: options.externalAttachments,
            directory,
        });
        const htmlStream = new html_formatter_1.CucumberHtmlStream();
        externaliseStream.pipe(htmlStream);
        on('message', (message) => {
            externaliseStream.write(message);
        });
        htmlStream.on('data', (chunk) => write(chunk));
        return async () => {
            externaliseStream.end();
            await (0, node_util_1.promisify)(node_stream_1.finished)(htmlStream);
        };
    },
    optionsKey: 'html',
};
//# sourceMappingURL=html.js.map