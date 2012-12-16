#This file is part of Tryton.  The COPYRIGHT file at the top level of
#this repository contains the full copyright notices and license terms.
import os
import time
import tempfile
import logging

from trytond.report import Report
from trytond.config import CONFIG
from trytond.pool import Pool
from trytond.transaction import Transaction
from trytond.cache import Cache

import JasperReports

# Determines the port where the JasperServer process should listen with its XML-RPC server for incomming calls
CONFIG['jasperport'] = CONFIG.get('jasperport', 8090)

# Determines the file name where the process ID of the JasperServer process should be stored
CONFIG['jasperpid'] = CONFIG.get('jasperpid', 'openerp-jasper.pid')

# Determines if temporary files will be removed
CONFIG['jasperunlink'] = CONFIG.get('jasperunlink', True)


class JasperReport(Report):
    _get_report_file_cache = Cache('jasper_report.report_file')

    @classmethod
    def write_properties(cls, filename, properties):
        text = u''
        for key, value in properties.iteritems():
            key = key.replace(':', '\\:').replace(' ', '\\ ')
            value = value.replace(':', '\\:').replace(' ', '\\ ')
            text += u'%s=%s\n' % (key, value)
        import codecs
        f = codecs.open(filename, 'w', 'utf-8')
        #f = open(filename, 'w')
        try:
            f.write(text)
        finally:
            f.close()

    @classmethod
    def get_report_file(cls, report):
        path = cls._get_report_file_cache.get(report.name)
        if path is not None:
            return path
        report_content = str(report.report_content)

        if not report_content:
            raise Exception('Error', 'Missing report file!')

        path = tempfile.mkdtemp(prefix='trytond-jasper-')
        fname = os.path.split(report.report)[-1]
        basename = fname.split('.')[0]
        jrxml_path = os.path.join(path, fname)
        f = open(jrxml_path, 'w')
        try:
            f.write(report_content)
        finally:
            f.close()

        Translation = Pool().get('ir.translation')
        translations = Translation.search([
                ('type', '=', 'jasper'),
                ('name', '=', report.report_name),
                ], order=[
                ('lang', 'ASC'),
                ])
        lang = None
        p = {}
        for translation in translations:
            if lang != translation.lang:
                if lang:
                    pfile = os.path.join(path, '%s_%s.properties' % (
                            basename, lang))
                    cls.write_properties(pfile, p)
                    #p.store(open(pfile, 'w'))
                lang = translation.lang
            if translation.src is None or translation.value is None:
                continue
            p[translation.src] = translation.value
        if lang:
            pfile = os.path.join(path, '%s_%s.properties' % (basename, lang))
            cls.write_properties(pfile, p)
        cls._get_report_file_cache.set(report.name, jrxml_path)
        return jrxml_path

    @classmethod
    def execute(cls, ids, data):
        pool = Pool()
        ActionReport = pool.get('ir.action.report')
        logger = logging.getLogger('jasper_reports')

        report_actions = ActionReport.search([
                ('report_name', '=', cls.__name__)
                ])
        if not report_actions:
            raise Exception('Error', 'Report (%s) not find!' % cls.__name__)
        report_action = report_actions[0]
        report_path = cls.get_report_file(report_action)
        report = JasperReports.JasperReport(report_path)
        model = report_action.model
        output_format = report_action.extension

        # Create temporary input (CSV) and output (PDF) files 
        temporary_files = []

        fd, dataFile = tempfile.mkstemp()
        os.close(fd)
        fd, outputFile = tempfile.mkstemp()
        os.close(fd)
        temporary_files.append(dataFile)
        temporary_files.append(outputFile)
        logger.info("Temporary data file: '%s'" % dataFile)

        start = time.time()

        # If the language used is xpath create the xmlFile in dataFile.
        if report.language() == 'xpath':
            if data.get('data_source','model') == 'records':
                generator = JasperReports.CsvRecordDataGenerator(report, 
                    data['records'])
            else:
                generator = JasperReports.CsvBrowseDataGenerator(report, model, 
                    ids)
            generator.generate(dataFile)
            temporary_files += generator.temporary_files
        
        subreportDataFiles = []
        for subreportInfo in report.subreports():
            subreport = subreportInfo['report']
            if subreport.language() == 'xpath':
                message = 'Creating CSV '
                if subreportInfo['pathPrefix']:
                    message += 'with prefix %s ' % subreportInfo['pathPrefix']
                else:
                    message += 'without prefix '
                message += 'for file %s' % subreportInfo['filename']
                logger.info(message)

                fd, subreportDataFile = tempfile.mkstemp()
                os.close(fd)
                subreportDataFiles.append({
                    'parameter': subreportInfo['parameter'],
                    'dataFile': subreportDataFile,
                    'jrxmlFile': subreportInfo['filename'],
                })
                temporary_files.append( subreportDataFile )

                if subreport.isHeader():
                    generator = JasperReports.CsvBrowseDataGenerator(subreport,
                        'res.users', [Transaction().user])
                elif data.get('data_source', 'model') == 'records':
                    generator = JasperReports.CsvRecordDataGenerator(subreport,
                        datas['records'])
                else:
                    generator = JasperReports.CsvBrowseDataGenerator(subreport,
                        model, ids)
                generator.generate(subreportDataFile)


        # Start: Report execution section
        locale = Transaction().language
        
        connectionParameters = {
            'output': output_format,
            'csv': dataFile,
            'dsn': cls.dsn(),
            'user': cls.userName(),
            'password': cls.password(),
            'subreports': subreportDataFiles,
        }
        parameters = {
            'STANDARD_DIR': report.standardDirectory(),
            'REPORT_LOCALE': locale,
            'IDS': ids,
        }
        if 'parameters' in data:
            parameters.update(data['parameters'])

        # Call the external java application that will generate the PDF file in outputFile
        server = JasperReports.JasperServer(int(CONFIG['jasperport']))
        server.setPidFile(CONFIG['jasperpid'])
        pages = server.execute(connectionParameters, report_path, outputFile, parameters)
        # End: report execution section

        elapsed = (time.time() - start) / 60
        logger.info("Elapsed: %.4f seconds" % elapsed )

        # Read data from the generated file and return it
        f = open( outputFile, 'rb')
        try:
            file_data = f.read()
        finally:
            f.close()

        # Remove all temporary files created during the report
        if CONFIG['jasperunlink']:
            for file in temporary_files:
                try:
                    os.unlink( file )
                except os.error, e:
                    logger = netsvc.Logger()
                    logger.warning("Could not remove file '%s'." % file)

        if Transaction().context.get('return_pages'):
            return (output_format, buffer(file_data), 
                report_action.direct_print, report_action.name, pages)

        return (output_format, buffer(file_data), report_action.direct_print,
            report_action.name)

    @classmethod
    def dsn(cls):
        host = CONFIG['db_host'] or 'localhost'
        port = CONFIG['db_port'] or '5432'
        dbname = Transaction().cursor.dbname
        return 'jdbc:postgresql://%s:%s/%s' % ( host, port, dbname )

    @classmethod
    def userName(cls):
        return CONFIG['db_user'] or cls.systemUserName()

    @classmethod
    def password(cls):
        return CONFIG['db_password'] or ''

    @classmethod
    def systemUserName(cls):
        if os.name == 'nt':
            import win32api
            return win32api.GetUserName()
        else:
            import pwd
            return pwd.getpwuid(os.getuid())[0]

    @classmethod
    def path(cls):
        return os.path.abspath(os.path.dirname(__file__))

    @classmethod
    def addonsPath(cls):
        return os.path.dirname(cls.path())
