package com.riftx.burp;

import burp.api.montoya.BurpExtension;
import burp.api.montoya.MontoyaApi;
import burp.api.montoya.http.message.HttpRequestResponse;
import burp.api.montoya.ui.contextmenu.ContextMenuEvent;
import burp.api.montoya.ui.contextmenu.ContextMenuItemsProvider;
import burp.api.montoya.ui.contextmenu.MessageEditorHttpRequestResponse;
import java.awt.BorderLayout;
import java.awt.Desktop;
import java.awt.GridLayout;
import java.net.URI;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import javax.swing.JButton;
import javax.swing.JCheckBox;
import javax.swing.JLabel;
import javax.swing.JMenuItem;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JTextArea;
import javax.swing.JTextField;
import javax.swing.SwingUtilities;

public final class RiftXBurpExtension implements BurpExtension, ContextMenuItemsProvider {
    private final ExecutorService workers = Executors.newCachedThreadPool();
    private MontoyaApi api;
    private final JTextField apiUrl = new JTextField("http://127.0.0.1:8787");
    private final JTextField runId = new JTextField();
    private final JTextField objective = new JTextField("Analyze captured Burp request");
    private final JTextField engagement = new JTextField("Burp connector capture");
    private final JCheckBox createNew = new JCheckBox("Create new Run", true);
    private final JTextArea progress = new JTextArea();

    @Override
    public void initialize(MontoyaApi api) {
        this.api = api;
        api.extension().setName("RiftX Connector");
        api.userInterface().registerSuiteTab("RiftX", panel());
        api.userInterface().registerContextMenuItemsProvider(this);
        api.logging().logToOutput("RiftX Connector initialized; it does not run an Agent runtime.");
    }

    @Override
    public List<java.awt.Component> provideMenuItems(ContextMenuEvent event) {
        Optional<MessageEditorHttpRequestResponse> editor =
                event.messageEditorRequestResponse();
        HttpRequestResponse selected = editor
                .map(MessageEditorHttpRequestResponse::requestResponse)
                .orElseGet(() -> event.selectedRequestResponses().stream().findFirst().orElse(null));
        if (selected == null) return List.of();
        JMenuItem send = new JMenuItem("Send request/response to RiftX");
        send.addActionListener(ignored -> submit(selected));
        return List.of(send);
    }

    private JPanel panel() {
        JPanel root = new JPanel(new BorderLayout(8, 8));
        JPanel fields = new JPanel(new GridLayout(0, 2, 6, 6));
        fields.add(new JLabel("RiftX API")); fields.add(apiUrl);
        fields.add(createNew); fields.add(new JLabel("Uncheck to append to Run ID"));
        fields.add(new JLabel("Existing Run ID")); fields.add(runId);
        fields.add(new JLabel("New Run objective")); fields.add(objective);
        fields.add(new JLabel("Engagement name")); fields.add(engagement);
        JPanel actions = new JPanel();
        JButton cancel = new JButton("Cancel Run");
        cancel.addActionListener(ignored -> workers.submit(this::cancel));
        JButton open = new JButton("Open WebUI");
        open.addActionListener(ignored -> workers.submit(this::openWebui));
        actions.add(cancel); actions.add(open);
        progress.setEditable(false);
        root.add(fields, BorderLayout.NORTH);
        root.add(new JScrollPane(progress), BorderLayout.CENTER);
        root.add(actions, BorderLayout.SOUTH);
        return root;
    }

    private void submit(HttpRequestResponse item) {
        workers.submit(() -> {
            try {
                byte[] request = item.request().toByteArray().getBytes();
                byte[] response = item.response() == null ? null : item.response().toByteArray().getBytes();
                HttpCapture capture = RawHttpParser.parse(item.request().url(), request, response);
                RiftXConnectorClient client = client();
                RiftXConnectorClient.Receipt receipt = createNew.isSelected()
                        ? client.submitNew(objective.getText(), engagement.getText(), capture)
                        : client.submitExisting(requiredRunId(), capture);
                runId.setText(receipt.runId());
                append("Imported " + capture.method() + " " + capture.url() + " into " + receipt.runId());
                workers.submit(() -> stream(receipt.runId()));
            } catch (Exception error) {
                append("Import failed: " + error.getMessage());
            }
        });
    }

    private void stream(String id) {
        try { client().streamEvents(id, this::append); }
        catch (Exception error) { append("SSE stopped: " + error.getMessage()); }
    }

    private void cancel() {
        try { client().cancel(requiredRunId()); append("Cancel requested for " + runId.getText()); }
        catch (Exception error) { append("Cancel failed: " + error.getMessage()); }
    }

    private void openWebui() {
        try { Desktop.getDesktop().browse(URI.create(client().webuiUrl(requiredRunId()))); }
        catch (Exception error) { append("Open WebUI failed: " + error.getMessage()); }
    }

    private RiftXConnectorClient client() { return new RiftXConnectorClient(apiUrl.getText().trim()); }

    private String requiredRunId() {
        String value = runId.getText().trim();
        if (value.isEmpty()) throw new IllegalStateException("Run ID is required");
        return value;
    }

    private void append(String message) {
        SwingUtilities.invokeLater(() -> progress.append(message + System.lineSeparator()));
    }
}
