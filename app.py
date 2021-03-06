import functools
import json
import os
import requests
import tempfile
import urllib.request
import zipfile
from flask import Flask, jsonify, make_response, abort, request
from werkzeug.utils import secure_filename

from AppData import AppData
from CommonFunctions import setInDict, getFilenameFromUrl, getFromDict, makeStrPathFromList, \
    getOntologyFilePath
from Constants import TRACKS, \
    EXPERIMENTS, SAMPLES, TERM_LABEL, \
    DOC_ONTOLOGY_VERSIONS_NAMES, FILE_NAME, VERSION_IRI, DOAP_VERSION, \
    EDAM_ONTOLOGY, \
    SAMPLE_TYPE_MAPPING, BIOSPECIMEN_CLASS_PATH, SAMPLE_TYPE_SUMMARY_PATH, \
    EXPERIMENT_TARGET_PATHS, \
    TARGET_DETAILS_PATH, TARGET_SUMMARY_PATH, TRACK_FILE_URL_PATH, \
    SPECIES_ID_PATH, \
    IDENTIFIERS_API_URL, RESOLVED_RESOURCES, NCBI_TAXONOMY_RESOLVER_URL, \
    SPECIES_NAME_PATH, SAMPLE_ORGANISM_PART_PATH, SAMPLE_DETAILS_PATH, \
    HAS_AUGMENTED_METADATA, SCHEMA_URL_PART1, SCHEMA_URL_PART2

app = Flask(__name__)
ontologies = {}


@app.route('/')
def index():
    return 'OK'


@app.errorhandler(400)
def custom400(error):
    response = jsonify({'message': error.description})
    return make_response(response, 400)


@app.route('/augment', methods=['POST'])
@app.route('/autogenerate', methods=['POST'])
def augment():
    if 'data' not in request.files:
        abort(400, 'Parameter called data containing fairtracks json data is required')
    dataJson = request.files['data']

    with tempfile.TemporaryDirectory() as tmpDir:
        dataFn = ''
        if dataJson:
            dataFn = secure_filename(dataJson.filename)
            dataJson.save(os.path.join(tmpDir, dataFn))

        with open(os.path.join(tmpDir, dataFn)) as dataFile:
            data = json.load(dataFile)

        appData = AppData(ontologies)

        if 'schemas' in request.files:
            file = request.files['schemas']
            filename = secure_filename(file.filename)
            file.save(os.path.join(tmpDir, filename))

            with zipfile.ZipFile(os.path.join(tmpDir, filename), 'r') as archive:
                archive.extractall(tmpDir)

            appData.initApp(data, tmpDir)
        else:
            appData.initApp(data)

        augmentFields(data, appData)

    return data


def addOntologyVersions(data, appData):
    # Very cumbersome way to support both v1 and v2 names. Should be
    # refactored. Also no good error message if no document info property is
    # found.
    for docInfoName in DOC_ONTOLOGY_VERSIONS_NAMES.keys():
        if docInfoName in data:
            docInfo = data[docInfoName]
            docOntologyVersionsName = DOC_ONTOLOGY_VERSIONS_NAMES[docInfoName]
            if docOntologyVersionsName not in docInfo:
                docInfo[docOntologyVersionsName] = {}

            docOntologyVersions = docInfo[docOntologyVersionsName]

    urlAndVersions = []
    for url, ontology in appData.getOntologies().items():
        fn = getOntologyFilePath(url)
        edam = False
        if EDAM_ONTOLOGY in url:
            edam = True
        with open(fn, 'r') as ontoFile:
            for line in ontoFile:
                if edam:
                    if DOAP_VERSION in line:
                        versionNumber = line.split(DOAP_VERSION)[1].split('<')[0]
                        versionIri = EDAM_ONTOLOGY + 'EDAM_' + versionNumber + '.owl'
                        urlAndVersions.append((url, versionIri))
                        break
                else:
                    if VERSION_IRI in line:
                        versionIri = line.split(VERSION_IRI)[1].split('"')[0]
                        urlAndVersions.append((url, versionIri))
                        break

    for url, versionIri in urlAndVersions:
        docOntologyVersions[url] = versionIri


def generateTermLabels(data, appData):
    for category in data:
        if not isinstance(data[category], list):
            continue
        for item in data[category]:
            for path, ontologyUrls in appData.getPathsWithOntologyUrls():
                if path[0] != category:
                    continue

                try:
                    termIdVal = getFromDict(item, path[1:])
                except KeyError:
                    continue

                termLabelVal = searchOntologiesForTermId(tuple(ontologyUrls), termIdVal, appData)

                if termLabelVal:
                    setInDict(item, path[1:-1] + [TERM_LABEL], termLabelVal)
                else:
                    abort(400, 'Item ' + termIdVal + ' not found in ontologies ' + str(ontologyUrls)
                          + ' (path in json: ' + makeStrPathFromList(path, category) + ')')


@functools.lru_cache(maxsize=50000)
def searchOntologiesForTermId(ontologyUrls, termIdVal, appData):
    termLabelVal = ''
    for url in ontologyUrls:
        ontology = appData.getOntologies()[url]
        termLabelSearch = ontology.search(iri=termIdVal)
        if termLabelSearch:
            termLabelVal = termLabelSearch[0].label[0]
        if termLabelVal:
            break
    return termLabelVal


def addSampleSummary(data):
    samples = data[SAMPLES]
    for sample in samples:
        biospecimenTermId = getFromDict(sample, BIOSPECIMEN_CLASS_PATH)
        if biospecimenTermId in SAMPLE_TYPE_MAPPING:
            sampleTypeVal = getFromDict(sample, SAMPLE_TYPE_MAPPING[biospecimenTermId])
            if TERM_LABEL in sampleTypeVal:
                summary = sampleTypeVal[TERM_LABEL]
                details = []

                try:
                    organismPart = getFromDict(sample, SAMPLE_ORGANISM_PART_PATH)
                    if summary != organismPart:
                        details.append(organismPart)
                except KeyError:
                    pass

                try:
                    sample_details = getFromDict(sample, SAMPLE_DETAILS_PATH)
                    details.append(sample_details)
                except KeyError:
                    pass

                if details:
                    summary = "{} ({})".format(summary, ', '.join(details))

                setInDict(sample, SAMPLE_TYPE_SUMMARY_PATH, summary)
        else:
            abort(400, 'Unexpected biospecimen_class term_id: ' + biospecimenTermId)


def addTargetSummary(data):
    experiments = data[EXPERIMENTS]
    val = ''
    for exp in experiments:
        for path in EXPERIMENT_TARGET_PATHS:
            try:
                val = getFromDict(exp, path)
                break
            except KeyError:
                continue

        if val:
            details = ''
            try:
                details = getFromDict(exp, TARGET_DETAILS_PATH)
            except KeyError:
                pass

            if details:
                val += ' (' + details + ')'
            setInDict(exp, TARGET_SUMMARY_PATH, val)


def addFileName(data):
    tracks = data[TRACKS]
    for track in tracks:
        fileUrl = getFromDict(track, TRACK_FILE_URL_PATH)
        fileName = getFilenameFromUrl(fileUrl)
        setInDict(track, TRACK_FILE_URL_PATH[:-1] + [FILE_NAME], fileName)


def addSpeciesName(data):
    samples = data[SAMPLES]
    for sample in samples:
        speciesId = getFromDict(sample, SPECIES_ID_PATH)
        speciesName = getSpeciesNameFromId(speciesId)
        setInDict(sample, SPECIES_NAME_PATH, speciesName)


@functools.lru_cache(maxsize=1000)
def getSpeciesNameFromId(speciesId):
    providerCode = resolveIdentifier(speciesId)
    speciesName = getSpeciesName(speciesId.split('taxonomy:')[1], providerCode)
    return speciesName


def resolveIdentifier(speciesId):
    url = IDENTIFIERS_API_URL + speciesId
    responseJson = requests.get(url).json()

    for resource in responseJson['payload'][RESOLVED_RESOURCES]:
        if 'providerCode' in resource:
            if resource['providerCode'] == 'ncbi':
                return resource['providerCode']


def getSpeciesName(speciesId, providerCode):
    if providerCode == 'ncbi':
        url = NCBI_TAXONOMY_RESOLVER_URL + '&id=' + str(speciesId)

        for i in range(3):
            try:
                responseJson = requests.get(url).json()
                speciesName = responseJson['result'][speciesId]['scientificname']
                break
            except KeyError:
                pass

        return speciesName


def setAugmentedDataFlag(data):
    for docInfoName in HAS_AUGMENTED_METADATA.keys():
        if docInfoName in data:
            data[docInfoName][HAS_AUGMENTED_METADATA[docInfoName]] = True


def augmentFields(data, appData):
    generateTermLabels(data, appData)
    addOntologyVersions(data, appData)
    addFileName(data)
    addSampleSummary(data)
    addTargetSummary(data)
    addSpeciesName(data)
    setAugmentedDataFlag(data)
    #print(json.dumps(data))


def initOntologies():
    print("initializing ontologies")
    i = 1
    currentSchemaUrl = ""
    with tempfile.TemporaryDirectory() as tmpDir:
        while True:
            schemaUrl = SCHEMA_URL_PART1 + "v" + str(i) + SCHEMA_URL_PART2
            try:
                schemaFn, _ = urllib.request.urlretrieve(schemaUrl, os.path.join(tmpDir, 'schema.json'))
                currentSchemaUrl = schemaUrl
                i += 1
            except:
                break

    data = {}
    data["@schema"] = currentSchemaUrl
    appData = AppData({})
    appData.initApp(data)

    return appData.getOntologies()


if __name__ == '__main__':
    ontologies = initOntologies()

    app.run(host='0.0.0.0')




